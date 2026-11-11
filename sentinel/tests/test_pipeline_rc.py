"""A simulated RC run's RC-only stages (1.9): lineage base, migration, chaos, soak and the Explorer, end to end."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
import pytest
import yaml

from sentinel import explorer_findings as ef
from sentinel import soak as sk
from sentinel.agent.dispatcher import CatalogEntry, NameCatalog, ToolCall
from sentinel.agent.session import ExplorerLoop, HandlerResult, ModelResponse, Observation, StepEventFactory
from sentinel.agent.tools import TOOLSET_SCHEMA_VERSION
from sentinel.agent.zoom import NATIVE
from sentinel.explorer_steering import steering_version, target_list
from sentinel.lint.repo import Repo
from sentinel.orchestrator import chaos as ch
from sentinel.orchestrator.clock import ManualClock
from sentinel.orchestrator.explorer import ExplorerBudget, ExplorerSession, session_window
from sentinel.orchestrator.gateway import DropLedger, Flag
from sentinel.orchestrator.rig import CrashSignal
from sentinel.pipeline import rc
from sentinel.rc_lineage import LineageSoak
from sentinel.records import Fingerprint, RunOutcome
from sentinel.scope import PlannedCheckpoint
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "example"
WORKSPACES = [yaml.safe_load(p.read_text()) for p in sorted((EXAMPLE / "corpus" / "workspaces").glob("*/entry.yaml"))]
SURFACES = [yaml.safe_load(p.read_text()) for p in sorted((EXAMPLE / "registry" / "surfaces").glob("**/*.yaml"))]
CONTROLS = frozenset(f"{s['id']}.{c['id']}" for s in SURFACES for c in s.get("controls") or ())
RELEASED = "released-1998.1.0-8b2d4f6"
RC_BUILD = "rc-1998.2.0-4f1c9a2"
TS = "2026-10-01T10:00:00Z"
WS = "ws.tier-a.active-trader"
CENSUS = ("census/trade.order-entry.stock/ws.tier-a.active-trader",
          "census/monitor.activity.position-statement/ws.tier-a.active-trader",
          "census/charts.flexible-grid.chart-cell/ws.tier-b.dense-charts")
SOAK_SURFACE = "trade.active-trader.ladder"
MATCH = {"build_id": RELEASED, "display": {"w": 1920, "h": 1080}, "data_epoch": "e-2026-11-11"}
FP = Fingerprint(run_id="x", side="candidate", rig_id="rig-05", read_at=TS, match=MATCH).fingerprint_id
STALE = Fingerprint(run_id="x", side="candidate", rig_id="rig-05", read_at=TS,
                    match={**MATCH, "data_epoch": "e-2026-10-01"}).fingerprint_id
PS = "monitor.activity.position-statement"
TOGGLE = f"{PS}.section-toggle"
MB = sk.MB


# -- the Central Store with the last release's lineage -------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "central.db")
    for rig, login, role, kind in (("rig-05", "paper-05", "pool", "scripted"), ("rig-06", "explorer-06", "explorer",
                                                                                "explorer")):
        s.register_rig(rig, login_alias=login, owner="ops", snapshot_id=f"snap-{rig}", allowlist_entry=int(rig[-1]),
                       role=role, login_kind=kind)
    for b, ch_, rel in (("main-1998.0.9-1111111", "main", None), (RELEASED, "released", "2026-10-15T00:00:00Z"),
                        (RC_BUILD, "rc", None)):
        s.register_build(b, channel=ch_, source_commit=b[-7:] * 5 + "x" * 5, pipeline_artifact_url=f"https://a/{b}",
                         installer_sha256="c" * 64, released_at=rel)
    yield s
    s.close()


def lineage(store, match=MATCH, approve=(CENSUS[0],), signatures=("com.tos.known.Frame.a",)):
    """The released build's own RC run: its soak, its approved baselines and its fingerprint."""
    pair = store.register_pair(RELEASED, "main-1998.0.9-1111111", "rc")
    soak_cps = tuple(sk.cycle_checkpoint(SOAK_SURFACE, n) for n in range(1, 25))
    scope = tuple(ScopeEntry(c, c.split("/")[2] if c.startswith("census/") else WS, "active_trader", 1, "masked_live",
                             flow_class="standard" if c.startswith("flow/") else None) for c in CENSUS + soak_cps)
    store.begin_run(RunPlan(run_id="rc-prev", run_type="rc", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                            snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                            budget_sec=8 * 3600, scope=scope, shards=(ShardPlan("rc-prev/1", "rig-05"),),
                            pair_id=pair, started_at=TS))
    store.write_fingerprint(Fingerprint(run_id="rc-prev", side="candidate", rig_id="rig-05", read_at=TS, match=match,
                                        shard_id="rc-prev/1"))
    start = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    store.write_soak_samples("rc-prev", [{"sampled_at": (start + timedelta(minutes=m)).isoformat(), "pid": 7,
                                          "metric": "working_set", "value": 2048 * MB} for m in range(1, 361, 30)])
    from sentinel.records import CheckpointOutcome
    for n, cid in enumerate(soak_cps, 1):
        sha = f"{n:064x}"
        store.insert("artifacts", {"sha256": sha, "kind": "capture", "media_type": "image/png", "bytes": 10,
                                   "redaction_passed": 1, "set_path": "runs/rc-prev", "created_at": TS})
        store.write_checkpoint(CheckpointOutcome(run_id="rc-prev", checkpoint_id=cid, outcome="identical",
                                                 workspace_id=WS, persona="active_trader", data_mode="masked_live",
                                                 artifacts={"candidate": sha}), shard_id="rc-prev/1")
    store.fill_unreached("rc-prev", "not_run", "budget")
    store.update_summary("rc-prev", {"rc": {"soak": {"log_signatures": list(signatures)}}})
    store.end_run(RunOutcome(run_id="rc-prev", run_type="rc", status="complete_with_env_errors", last_gate="reported"))
    for cid in approve:
        store.insert("baselines", {"checkpoint_id": cid, "lineage": "released", "build_id": RELEASED,
                                   "image_sha256": "e" * 64, "state": "approved", "source_run_id": "rc-prev",
                                   "proposed_at": TS, "approved_by": "jdoe", "approved_at": TS,
                                   "approval_reason": "release sign-off"})


# -- simulated rigs ----------------------------------------------------------------------------------------------
@dataclass
class SimMigration:
    loads: dict = field(default_factory=dict)
    asked: list = field(default_factory=list)

    def load_on_candidate(self, workspace_id):
        self.asked.append(workspace_id)
        return self.loads.get(workspace_id, rc.MigrationLoad(True, (
            rc.MigrationDialog("Workspace conversion", "dialog.order-confirmation", "f" * 64),)))


@dataclass
class SimSoak:
    """Rig 5: a working set that grows `ws_per_h` MB/h, freezes at cycles in `freeze_at`, crashes at `crash_at`,
    stops after `stop_after`, and logs `log` (None: not located)."""

    ws_per_h: float = 0.0
    freeze_at: tuple[int, ...] = ()
    crash_at: tuple[int, ...] = ()
    stop_after: int | None = None
    log: dict = field(default_factory=dict)
    log_located: bool = True

    def run_cycle(self, n):
        if self.stop_after is not None and n > self.stop_after:
            return None
        lo, hi = (n - 1) * 900, n * 900
        samples = tuple(sk.Sample(float(t), 4100, "working_set", 2048 * MB + self.ws_per_h * MB * t / 3600)
                        for t in range(lo + 60, hi + 1, 60))
        clock = tuple(sk.ClockSample(float(t), "frozen" if n in self.freeze_at and 100 <= t - lo <= 140 else f"h{t}")
                      for t in range(lo, hi, 2))
        crash = CrashSignal("jvm_crash", "crash_file") if n in self.crash_at else None
        text = self.log.get(n, "") if self.log_located else None
        return rc.SoakCycle(f"{n:02d}" * 32, samples, clock, crash, text)


def context(**kw) -> rc.RcContext:
    plan = ch.ChaosPlan(PS, WS, "active_trader", "tos-login.example")
    base = dict(run_id="rc-now", fingerprint_id=FP, scope=CENSUS, workspaces=WORKSPACES, soak_surface_id=SOAK_SURFACE,
                chaos_plan=plan)
    base.update(kw)
    return rc.RcContext(**base)


def ports(**kw) -> rc.RcPorts:
    base = dict(migration=SimMigration(), soak=SimSoak(), shaper=ch.SimulatedShaper(), chaos=ch.SimulatedChaosDriver())
    base.update(kw)
    return rc.RcPorts(**base)


SOAK_META = rc.soak_meta(WORKSPACES, "ws.tier-b.active-dense")


# -- the stages through run_rc -----------------------------------------------------------------------------------
def test_a_matching_lineage_is_the_base_and_its_missing_checkpoint_is_lineage_missing(store):
    lineage(store)
    res = rc.run_rc(store, context(), ports())
    assert res.base.mode == "stored" and res.base.base_image(CENSUS[0]) == "e" * 64
    assert (res.outcomes[CENSUS[1]].outcome, res.outcomes[CENSUS[1]].reason) == ("needs_baseline", "lineage_missing")
    planned = {CENSUS[1]: PlannedCheckpoint(CENSUS[1], PS, "census", 1, WS, "active_trader", "masked_live")}
    recs, unplaced = res.records("rc-now", SOAK_META, planned)
    assert CENSUS[2] in unplaced and CENSUS[1] not in unplaced
    assert next(r for r in recs if r.checkpoint_id == CENSUS[1]).reason == "lineage_missing"


def test_a_lineage_fingerprint_mismatch_triggers_a_recapture(store):
    lineage(store, match={**MATCH, "data_epoch": "e-2026-10-01"})
    res = rc.run_rc(store, context(), ports())
    assert res.base.mode == "recapture" and set(res.base.recapture) == set(CENSUS)
    assert res.base.outcomes() == {} and CENSUS[1] not in res.outcomes
    assert res.health[0].startswith("RC base: released-1998.1.0-8b2d4f6 re-captured")
    assert not res.soak.summary["lineage_comparable"]                 # the soak lineage is on the old fingerprint too
    cid = sk.cycle_checkpoint(SOAK_SURFACE, 1)
    assert (res.outcomes[cid].outcome, res.outcomes[cid].reason) == ("needs_baseline", "lineage_missing")


def test_a_migration_dialog_is_captured_as_a_checkpoint(store):
    lineage(store)
    unmatched = rc.MigrationLoad(True, (rc.MigrationDialog("Convert layout?", None, "9" * 64),))
    sim = SimMigration({"ws.tier-b.options-sidebar": unmatched,
                        "ws.tier-b.dense-charts": rc.MigrationLoad(False, detail="load timed out")})
    res = rc.run_rc(store, context(), ports(migration=sim))
    assert sorted(sim.asked) == ["ws.tier-b.dense-charts", "ws.tier-b.options-sidebar"]   # saved under the release
    [cp] = res.migration.checkpoints
    assert cp.checkpoint_id == "flow/dialog.migration-unmatched/flow.migration.ws.tier-b.options-sidebar/d01"
    assert (res.outcomes[cp.checkpoint_id].outcome, res.outcomes[cp.checkpoint_id].reason) == ("needs_baseline",
                                                                                                "new_surface")
    assert [f.kind for c, f in res.findings() if c == cp.checkpoint_id] == ["unknown_popup"]
    o = res.outcomes[CENSUS[2]]                                         # the census of the workspace that did not load
    assert (o.outcome, o.gate, o.reason) == ("env_error", "workspace_loaded", "precondition:workspace_loaded")
    recs, _ = res.records("rc-now", SOAK_META)
    rec = next(r for r in recs if r.checkpoint_id == cp.checkpoint_id)
    assert rec.flow_id == "flow.migration.ws.tier-b.options-sidebar" and rec.step_id == "d01"
    assert rec.artifacts == {"candidate": "9" * 64} and rec.tier == 1


def test_a_chaos_scenario_without_its_observation_is_chaos_unobserved(store):
    lineage(store)
    driver = ch.SimulatedChaosDriver({"disconnect-30s": None})
    res = rc.run_rc(store, context(), ports(chaos=driver))
    cid = ch.SCENARIOS[3].checkpoint_id(PS)
    assert (res.outcomes[cid].outcome, res.outcomes[cid].reason) == ("harness_error", "chaos_unobserved")
    recs, _ = res.records("rc-now", SOAK_META)
    assert next(r for r in recs if r.checkpoint_id == cid).tier == 2
    assert res.sections()["connectivity"][3]["outcome"] == "harness_error"


def test_a_soak_working_set_growth_over_budget_is_assertion_failed_at_tier_1(store):
    lineage(store)
    res = rc.run_rc(store, context(), ports(soak=SimSoak(ws_per_h=600)))
    cycles = [sk.cycle_checkpoint(SOAK_SURFACE, n) for n in range(1, 25)]
    first = next(c for c in cycles if c in res.outcomes)
    o = res.outcomes[first]
    assert (o.outcome, o.reason) == ("assertion_failed", "assertion:soak.working_set")
    assert first == sk.cycle_checkpoint(SOAK_SURFACE, 6)              # 25% above the hour-1 median
    recs, _ = res.records("rc-now", SOAK_META)
    rec = next(r for r in recs if r.checkpoint_id == first)
    assert rec.tier == 1 and rec.flow_id == "soak" and rec.step_id == "c06" and "soak.working_set" in rec.assertion_ids
    assert rec.artifacts == {"candidate": "06" * 32}
    c12 = sk.cycle_checkpoint(SOAK_SURFACE, 12)
    assert "soak.growth_slope" in res.soak.assertion_ids[c12]


def test_a_comparable_lineage_soak_gives_each_cycle_its_base_capture(store):
    lineage(store)
    res = rc.run_rc(store, context(), ports())
    c3 = sk.cycle_checkpoint(SOAK_SURFACE, 3)
    assert res.soak.summary["lineage_comparable"] and res.soak.base_captures[c3] == f"{3:064x}"
    assert res.soak.outcomes[c3] is None                                 # the normal diff path decides
    assert res.soak.section["lineage_curves"] is not None and res.soak.section["gate_line"] == "soak: 24 cycles run"


def test_a_freeze_in_soak_is_the_app_failure_trio_and_breaches_freezes_and_relaunches():
    res = rc.run_soak(SimSoak(freeze_at=(3,)), SOAK_SURFACE, LineageSoak(None, None, False), sk.SoakBudgets())
    c3 = sk.cycle_checkpoint(SOAK_SURFACE, 3)
    o = res.outcomes[c3]
    assert (o.outcome, o.reason, o.detail) == ("assertion_failed", "assertion:app_alive", "hang")
    assert res.assertion_ids[c3] == ("soak.freezes", "soak.relaunches", "soak.render_latency")
    assert [f.kind for c, f in res.findings if c == c3] == ["app_crash"]
    assert res.consequences[0].has("relaunch") and res.summary["freezes"] == 1 and res.run is None
    assert res.outcomes[sk.cycle_checkpoint(SOAK_SURFACE, 4)].reason == "lineage_missing"   # the loop goes on


def test_the_fourth_app_failure_ends_the_soak_and_the_run():
    res = rc.run_soak(SimSoak(crash_at=(2, 3, 4, 6)), SOAK_SURFACE, LineageSoak(None, None, True), sk.SoakBudgets())
    assert (res.run.status, res.run.reason) == ("aborted_budget", "app_restart_budget")
    assert all((res.outcomes[sk.cycle_checkpoint(SOAK_SURFACE, n)].outcome,
                res.outcomes[sk.cycle_checkpoint(SOAK_SURFACE, n)].reason) == ("not_run", "budget")
               for n in range(7, 25))
    assert res.summary["cycles_reached"] == 6 and "not finished, 18 of 24" in res.section["gate_line"]


def test_an_unfinished_soak_is_not_run_budget_per_unreached_cycle():
    res = rc.run_soak(SimSoak(stop_after=20), SOAK_SURFACE, LineageSoak(None, None, True), sk.SoakBudgets())
    assert [c for c, o in res.outcomes.items() if o and o.outcome == "not_run"] == \
        [sk.cycle_checkpoint(SOAK_SURFACE, n) for n in range(21, 25)]
    rows = res.sample_rows(datetime(2026, 11, 11, 18, tzinfo=timezone.utc))
    assert rows[0]["sampled_at"] == "2026-11-11T18:01:00Z" and len(rows) == 20 * 15


LOG = "java.lang.IllegalStateException: x\n\tat com.tos.new.Thing.run(Thing.java:3)\n"
KNOWN = "java.lang.NullPointerException\n\tat com.tos.known.Frame.a(Frame.java:1)\n"


def test_soak_log_signatures_raise_a_finding_where_first_seen_and_feed_the_next_lineage(store):
    lineage(store)
    res = rc.run_rc(store, context(), ports(soak=SimSoak(log={2: KNOWN, 7: LOG, 9: LOG})))
    c7 = sk.cycle_checkpoint(SOAK_SURFACE, 7)
    [(cid, f)] = [(c, f) for c, f in res.findings() if f.kind == "new_log_signature"]
    assert cid == c7 and f.tier == 2 and "count 2" in f.detail
    assert res.summary()["rc"]["soak"]["log_signatures"] == ["com.tos.known.Frame.a", "com.tos.new.Thing.run"]
    missing = rc.run_soak(SimSoak(log_located=False), SOAK_SURFACE, LineageSoak(None, None, True), sk.SoakBudgets())
    assert sk.NOT_AVAILABLE in missing.health and missing.section["log_signatures_line"] == sk.NOT_AVAILABLE


# -- the Explorer stage ------------------------------------------------------------------------------------------
PIN = "claude-pinned-1"


@dataclass
class Model:
    def respond(self, *, system, messages, tools):
        return ModelResponse(PIN, "tool_use", (ToolCall("click_named", {"target": TOGGLE}, "c1"),), "expect toggle",
                             {"input_tokens": 5})


@dataclass
class Handler:
    clock: ManualClock
    n: int = 0

    def perform(self, request, profile):
        self.n += 1
        self.clock.advance(1.0)
        obs = (Observation(PS, "INV-03", "c" * 64),) if self.n == 3 else ()
        return HandlerResult(point=(10, 10), rect=(0, 0, 20, 10), post_hash=f"h{self.n}",
                             stable_state=PS if self.n == 1 else None, observations=obs,
                             click_crop_sha256="d" * 64)

    def screenshot(self):
        return np.zeros((NATIVE[1], NATIVE[0], 3), dtype=np.uint8)

    def catalog(self):
        return NameCatalog([CatalogEntry("Positions", ref=TOGGLE)])

    def flag(self):
        return Flag.CLEAR


class Runner:
    def __init__(self, invariants=("INV-03",)):
        self.invariants = invariants

    def replay(self, flow):
        return ef.ReplayResult(invariant_ids=self.invariants, capture_sha256="b" * 64)


@lru_cache(maxsize=1)
def example_repo() -> Repo:
    return Repo.load(EXAMPLE)


def stage(**kw) -> rc.ExplorerStage:
    clock = ManualClock()
    t0 = datetime(2026, 11, 11, 18, tzinfo=timezone.utc)
    budget = ExplorerBudget(max_actions=5)

    def loop(window, targets):
        session = ExplorerSession("exp-rc-now", clock, budget=budget, ledger=DropLedger())
        return ExplorerLoop(Model(), Handler(clock), session, PIN, StepEventFactory("rc-now", "rc-now/6", clock),
                            lambda r, e, res: True, lambda usage: 0.5)

    repo = example_repo()
    targets = target_list(SURFACES, {}, ())
    base = dict(branches={"computer_use": {"branch": "A"}}, agent_available=True, deny_test_passed=True, history=[],
                steering=targets, window=session_window(t0 + timedelta(hours=6, minutes=20), t0 + timedelta(hours=7,
                                                                                                           minutes=50)),
                loop=loop, runner=Runner(), lint=lambda f: ef.lint_candidate(repo, f, today=date(2026, 11, 10)),
                flow_ctx=ef.FlowContext("explorer-owner", "active_trader", WS, "fx.rig-01.paper", budget=budget),
                controls=CONTROLS, build_id=RC_BUILD, teardown=lambda: ((), {}))
    base.update(kw)
    return rc.ExplorerStage(**base)


def test_a_session_whose_finding_reproduces_reports_through_the_replayed_checkpoint():
    out = rc.run_explorer(stage(), "rc-now")
    assert out.ran and out.session.budget_hit == "actions" and out.session.actions == 5
    [r] = out.replayed
    assert r.replayed == "reproduced" and r.flow.path == "flows/quarantine/exp-rc-now-0003.yaml"
    o = out.outcomes[r.flow.checkpoint_id]
    assert (o.outcome, o.reason) == ("invariant_violation", "INV-03")
    assert out.meta[r.flow.checkpoint_id].flow_class == "explorer_candidate"
    assert out.captures[r.flow.checkpoint_id] == "b" * 64
    assert out.metrics.precision == 1.0 and out.metrics.toolset_schema_version == TOOLSET_SCHEMA_VERSION
    assert out.section["reproduced"][0]["checkpoint_id"] == r.flow.checkpoint_id
    [f] = out.findings("rc-now", lambda r: "f1")
    assert f.explorer.replayed == "reproduced" and not f.held
    assert any(line.startswith("Explorer budget hit: actions") for line in out.health)
    assert all(e.mode == "explorer" for e in out.session.events)


def test_a_finding_that_does_not_reproduce_is_listed_and_held():
    out = rc.run_explorer(stage(runner=Runner(invariants=())), "rc-now")
    assert out.outcomes == {} and out.metrics.precision == 0.0
    [row] = out.section[ef.NOT_REPRODUCED]
    assert row["held_as"] == "expected_behavior"
    [f] = out.findings("rc-now", lambda r: "f2")
    assert f.held and f.triage_state == "expected_behavior"


def test_a_signature_seen_before_keeps_its_finding_id_and_opens_none():
    key = ef.signature_key({"surface_id": PS, "observation": "INV-03", "crop_sha256": "c" * 64})
    prior = {key: ef.PriorFinding("41", key, "rc-old", "rc-old")}
    out = rc.run_explorer(stage(prior=prior), "rc-now")
    assert out.replayed[0].flow.path == "flows/quarantine/41.yaml" and out.findings("rc-now", lambda r: "x") == []


@pytest.mark.parametrize("kw,line,outcome", [
    ({"branches": {"computer_use": {"branch": "B"}}}, "Explorer: not available under DR-07 B", None),
    ({"deny_test_passed": False}, "denied-action test", ("not_run", "quarantine")),
    ({"agent_available": False}, "Explorer: not run, model endpoint unavailable", ("not_run", "model_unavailable")),
    ({"rig_clean": False}, "chaos rule", None)])
def test_the_stage_does_not_start_when_it_may_not(kw, line, outcome):
    out = rc.run_explorer(stage(**kw), "rc-now")
    assert not out.ran and line in out.line and out.section == {"line": out.line}
    got = out.outcomes.get("stage:explorer_stage")
    assert (got.outcome, got.reason) == outcome if outcome else got is None


def test_two_low_precision_runs_pause_the_explorer():
    targets = target_list(SURFACES, {}, ())
    sv = steering_version(targets)
    hist = [{"precision": 0.25, "steering_version": sv, "toolset_schema_version": TOOLSET_SCHEMA_VERSION},
            {"precision": 0.2, "steering_version": sv, "toolset_schema_version": TOOLSET_SCHEMA_VERSION}]
    out = rc.run_explorer(stage(history=hist), "rc-now")
    assert not out.ran and out.line.startswith("Explorer: paused")


def test_a_teardown_that_cannot_delete_blocks_the_explorer_login():
    out = rc.run_explorer(stage(teardown=lambda: (("alert AAPL",), {"alert AAPL": False})), "rc-now")
    o = out.outcomes["stage:explorer_stage"]
    assert (o.outcome, o.gate, o.reason) == ("env_error", "teardown", "precondition:explorer_ledger_clean")
    assert out.consequences[-1].has("block_login")


def test_the_explorer_history_feeds_the_pause_rule_from_the_store(store):
    lineage(store)
    store.update_summary("rc-prev", {"rc": {"explorer": {"precision": 0.1, "steering_version": "s",
                                                         "toolset_schema_version": "t"}}})
    assert rc.explorer_history(store) == [{"precision": 0.1, "steering_version": "s", "toolset_schema_version": "t"}]
    assert rc.explorer_history(store, before="2026-01-01T00:00:00Z") == []


def test_run_rc_with_the_explorer_writes_its_summary_and_sections(store):
    lineage(store)
    res = rc.run_rc(store, context(explorer=stage()), ports())
    block = res.summary()["rc"]
    assert set(block) == {"lineage", "soak", "migration", "chaos", "explorer"} and block["explorer"]["precision"] == 1.0
    assert set(res.sections()) == {"lineage", "migration", "connectivity", "soak", "explorer"}
    assert res.run is None and any(h.startswith("Explorer: precision") for h in res.health)
    recs, _ = res.records("rc-now", SOAK_META)
    [cand] = [r for r in recs if r.flow_class == "explorer_candidate"]
    assert (cand.outcome, cand.reason, cand.artifacts) == ("invariant_violation", "INV-03", {"candidate": "b" * 64})
    off = rc.run_rc(store, context(explorer=stage(branches={"computer_use": {"branch": "B"}})), ports())
    assert off.summary()["rc"]["explorer"] == {"ran": False, "line": "Explorer: not available under DR-07 B"}
    shaped = rc.run_rc(store, context(explorer=stage()), ports(shaper=ch.SimulatedShaper(stuck_clears=9)))
    assert not shaped.explorer.ran and not shaped.chaos.rig_clean
