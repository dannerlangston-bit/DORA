"""Evaluation inputs: settle parameters from config (3.5, 4.5, 4.7), the step-failure rules (3.1, 2.4, 2.6.1, SD-07),
input B and the base re-capture (3.11), the targeted re-run (3.10), and the SD-21 landmark refusal."""
import copy
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from sentinel import synth
from sentinel.classify import Condition, EnvFailure, Facts, classify
from sentinel.diff.engine import DiffContext, diff_pair
from sentinel.diff.regions import Landmark, Rect, Region, RegionsFile
from sentinel.diff.settle import Anchor, Frame, RerunBudget, SettleSpec, settle
from sentinel.evaluation import (
    BaseEvidence, CaptureFailure, b_input_reason, base_side_outcome, declares_new_surface, flake_event,
    recapture_plan, rerun_stability, settle_base, settle_spec_for, take_recaptures,
)
from sentinel.records import StepOutcome
from sentinel.scope import Manifest, ManifestEntry, PlannedCheckpoint
from sentinel.store.store import RunPlan, ScopeEntry, ShardPlan, Store

SIZE = (640, 400)
LAYOUT = synth.sample_layout(0)
SCREEN = synth.render(LAYOUT, SIZE)
IMG = SCREEN.image
_LX, _LY, _, _ = SCREEN.get("watchlist.title.label").bbox
LM_POS = (_LX - 2, _LY - 2)
CLOCK = Rect(560, 4, 70, 16)
WS = "ws.tier-a.active-trader"


def frames(images, start=0, step=250, **kw):
    return [Frame(start + i * step, im, **kw) for i, im in enumerate(images)]


def unstable_until(t_ms, then=3):
    """Frames that differ every 250 ms until `t_ms`, then `then` identical frames."""
    n = t_ms // 250
    return frames([synth.perturb(IMG, s, 20, 90) for s in range(n)]) + frames([IMG] * then, start=n * 250)


def checkpoint(surface="gadget.one", kind="census", flow=None, step=None):
    cid = f"flow/{surface}/{flow}/{step}" if kind == "flow" else f"{kind}/{surface}/{WS}"
    return PlannedCheckpoint(cid, surface, kind, 2, WS, "active_trader", "frozen", flow_id=flow, step_id=step)


def config(tmp_path, surfaces=(), flows=()):
    return SimpleNamespace(surfaces={s["id"]: s for s in surfaces}, flow_docs={f["flow_id"]: f for f in flows},
                           root=tmp_path)


def surface(sid="gadget.one", kind="gadget", **kw):
    return {"id": sid, "kind": kind, "landmarks": [], "anti_anchors": [], "settle": {}, **kw}


def regfile(cid="census/gadget.one/" + WS, regions=()):
    lm = Landmark("lm.watchlist", SCREEN.crop("watchlist.title.label", 2), expected=LM_POS)
    return RegionsFile(cid, [lm], list(regions))


# -- 3.5 settle parameters from the configuration ---------------------------------------------------------------

def test_chart_and_scan_checkpoints_wait_30_s_and_others_15_s(tmp_path):
    cfg = config(tmp_path, [surface("charts.grid.chart-cell", "chart"), surface("scan.stock-hacker"),
                            surface("monitor.activity.positions")])
    late = unstable_until(20000)
    chart = settle_spec_for(cfg, checkpoint("charts.grid.chart-cell"))
    scan = settle_spec_for(cfg, checkpoint("scan.stock-hacker"))
    census = settle_spec_for(cfg, checkpoint("monitor.activity.positions"))
    assert chart.long_wait and scan.long_wait and not census.long_wait
    assert chart.max_wait_ms is None and census.max_wait_ms is None      # the 3.5 defaults apply (choice 1)
    assert settle(late, chart).settled and settle(late, scan).settled
    res = settle(late, census)
    assert not res.settled and res.reason == "frames_unstable" and res.elapsed_ms == 15000


def test_checkpoint_max_settle_ms_overrides_the_surface_settle(tmp_path):
    flow = {"flow_id": "flow.x", "steps": [{"id": "cp1", "type": "checkpoint", "max_settle_ms": 2000},
                                           {"id": "cp2", "type": "checkpoint"}]}
    cfg = config(tmp_path, [surface(settle={"max_wait_ms": 8000})], [flow])
    seq = unstable_until(4000)
    surf = settle_spec_for(cfg, checkpoint("gadget.one", "flow", "flow.x", "cp2"))
    short = settle_spec_for(cfg, checkpoint("gadget.one", "flow", "flow.x", "cp1"))
    assert (surf.max_wait_ms, short.max_wait_ms) == (8000, 2000)
    assert settle(seq, surf).settled
    res = settle(seq, short)
    assert not res.settled and res.elapsed_ms == 2000
    census = settle_spec_for(cfg, checkpoint("gadget.one"))           # a census checkpoint has no max_settle_ms
    assert census.max_wait_ms == 8000


def test_surface_and_platform_anti_anchors(tmp_path):
    cfg = config(tmp_path, [surface(anti_anchors=["No data", "loading"])])
    spec = settle_spec_for(cfg, checkpoint())
    names = [a.name for a in spec.anti_anchors]
    assert names == ["Loading", "Reconnecting", "Disconnected", "No data"]     # 3.5 texts kept, duplicates dropped
    res = settle(frames([IMG] * 4, texts=("No data",)), spec)
    assert res.reason == "anti_anchor_present" and "No data" in res.detail
    spinner = Anchor("spinner", template=SCREEN.crop("positions.title.label", 1))
    with_spinner = settle_spec_for(cfg, checkpoint(), anti_anchor_templates=[spinner])
    res = settle(frames([IMG] * 4), with_spinner)
    assert res.reason == "anti_anchor_present" and "spinner" in res.detail
    assert settle(frames([IMG] * 4), spec).settled


def test_surface_must_see_landmarks_are_anchors(tmp_path):
    path = tmp_path / "registry" / "templates" / "positions.png"
    path.parent.mkdir(parents=True)
    cv2.imwrite(str(path), cv2.cvtColor(SCREEN.crop("positions.title.label", 1), cv2.COLOR_RGB2BGR))
    entry = surface(landmarks=[
        {"id": "lm.positions", "template": "registry/templates/positions.png", "must_see": True},
        {"id": "lm.decor", "template": "registry/templates/absent.png", "must_see": False},    # never read
    ])
    cfg = config(tmp_path, [entry])
    no_positions = copy.deepcopy(LAYOUT)
    synth.node(no_positions, "positions")["title"] = ""
    erased = synth.render(no_positions, SIZE).image
    spec = settle_spec_for(cfg, checkpoint(), regfile=regfile())
    assert [a.name for a in spec.anchors] == ["lm.positions"]          # the regions file's landmark is no anchor
    assert settle(frames([IMG] * 4), spec).settled
    res = settle(frames([erased] * 4), spec)
    assert res.reason == "anchor_missing" and "lm.positions" in res.detail
    # a resolver that cannot show the template (the simulation) leaves that anchor out
    sim = settle_spec_for(cfg, checkpoint(), regfile=regfile(), template=lambda lm_id, p: None)
    assert sim.anchors == [] and settle(frames([erased] * 4), sim).settled
    with pytest.raises(ValueError, match="not readable"):
        settle_spec_for(config(tmp_path, [surface(landmarks=[{"id": "lm.x", "template": "nope.png",
                                                              "must_see": True}])]), checkpoint())


def test_keep_focus_and_the_flows_neutral_control(tmp_path):
    flow = {"flow_id": "flow.x",
            "normalize": [{"id": "n1", "type": "park_mouse"},
                          {"id": "n2", "type": "focus_neutral", "target": "shell.status-bar.neutral"}],
            "steps": [{"id": "cp1", "type": "checkpoint", "keep_focus": True}, {"id": "cp2", "type": "checkpoint"}]}
    cfg = config(tmp_path, [surface()], [flow])
    focused = settle_spec_for(cfg, checkpoint("gadget.one", "flow", "flow.x", "cp1"))
    plain = settle_spec_for(cfg, checkpoint("gadget.one", "flow", "flow.x", "cp2"))
    held = frames([IMG] * 4, focus_dropped=False)
    assert focused.keep_focus and settle(held, focused).settled
    res = settle(held, plain)
    assert not plain.keep_focus and res.reason == "anchor_missing" and "shell.status-bar.neutral" in res.detail
    assert settle_spec_for(cfg, checkpoint()).neutral_control == SettleSpec().neutral_control


def test_masks_follow_their_landmark(tmp_path):
    clock = Region("rgn.title.clock", "mask", "lm.watchlist", Rect(CLOCK.x - LM_POS[0], CLOCK.y - LM_POS[1], 70, 16))
    cfg = config(tmp_path, [surface()])
    spec = settle_spec_for(cfg, checkpoint(), regfile=regfile(regions=[clock]), park=(630, 395))
    assert list(spec.ignore) == [CLOCK] and spec.park == (630, 395)
    moved = settle_spec_for(cfg, checkpoint(), regfile=regfile(regions=[clock]),
                            positions={"lm.watchlist": (LM_POS[0] + 5, LM_POS[1])})
    assert list(moved.ignore) == [CLOCK.offset(5, 0)]
    ticking = []
    for i in range(4):
        f = IMG.copy()
        f[CLOCK.y:CLOCK.y + CLOCK.h, CLOCK.x:CLOCK.x + CLOCK.w] = 30 + 10 * i
        ticking.append(f)
    assert settle(frames(ticking), spec).settled
    assert not settle(frames(ticking), settle_spec_for(cfg, checkpoint(), regfile=regfile())).settled


# -- 3.1, 2.4, 2.6.1 step failures (SD-07) ----------------------------------------------------------------------

def step(step_id, status="passed", side="candidate", **kw):
    return StepOutcome("run-1", "flow.x", step_id, status, side=side, **kw)


def manifest(*entries):
    return Manifest("mf-c", "cand", "base", "manual", "jdoe", tuple(entries))


NEW = ManifestEntry("tos-1-1", ("TOS-1",), declared_surfaces=("gadget.new",), new_surface=True)
OLD = ManifestEntry("tos-2-1", ("TOS-2",), declared_surfaces=("gadget.new", "gadget.old"))
INV = dict(invariant_ids=("INV-10",), invariant_stability="stable")


def test_a_base_only_step_failure_on_a_new_surface_is_needs_baseline():
    results = [step("s1"), step("s1", side="base"), step("s2"), step("s2", "failed", "base", pre_screenshot="b2")]
    v = base_side_outcome(results, manifest(OLD, NEW), "gadget.new")
    assert (v.case, v.step_id, v.candidate_checks) == ("base", "s2", True)
    c = classify(Facts(**v.facts, manifest="declared"))
    assert (c.row, c.outcome, c.reason, c.side) == ("9", "needs_baseline", "new_surface", "base")
    # the candidate capture still runs the Invariant Checker: a fired invariant outranks needs_baseline (row 7)
    assert classify(Facts(**v.facts, **INV, manifest="declared")).outcome == "invariant_violation"
    assert v.evidence == {"step_id": "s2", "side": "base", "pre_screenshot": None, "base_screenshot": "b2"}


def test_a_base_only_step_failure_otherwise_is_not_run_base_step_failed():
    results = [step("s1"), step("s1", "blocked_by_gateway", "base")]
    for m, sid in ((manifest(OLD, NEW), "gadget.old"), (manifest(OLD), "gadget.new"), (None, "gadget.new"),
                   (manifest(NEW), "gadget.undeclared")):
        v = base_side_outcome(results, m, sid)
        c = classify(Facts(**v.facts, **INV))
        assert (v.case, c.row, c.outcome, c.reason, c.side) == ("base", "3", "not_run", "base_step_failed", "base")
        assert any("invariant_violation" in e for e in c.evidence)          # listed although the outcome is not_run
    assert declares_new_surface(manifest(OLD, NEW), "gadget.new") and not declares_new_surface(None, "gadget.new")


def test_a_candidate_step_failure_is_the_candidate_rule():
    results = [step("s1", side="base", post_screenshot="b1-post"), step("s1", "failed", pre_screenshot="c1")]
    led = base_side_outcome(results, manifest(NEW), "gadget.new", led_to=True)
    c = classify(Facts(**led.facts))
    assert (led.case, c.outcome, c.reason, c.side) == ("candidate", "landmark_missing", "locator:s1", "candidate")
    assert led.evidence["pre_screenshot"] == "c1" and led.evidence["base_screenshot"] == "b1-post"
    later = base_side_outcome(results, manifest(NEW), "gadget.new")
    c = classify(Facts(**later.facts))
    assert (c.outcome, c.reason, later.candidate_checks) == ("not_run", "step_failed:s1", False)
    # the Executor's cause replaces the locator default on the checkpoint the step led to
    c = classify(Facts(**base_side_outcome(results, None, "gadget.new", led_to=True, assertion_ids=("qty",)).facts))
    assert (c.outcome, c.reason) == ("assertion_failed", "assertion:qty")
    env = EnvFailure("connectivity", "maintenance")
    with_env = base_side_outcome(results, None, "gadget.new", led_to=True, env=env)
    assert classify(Facts(**with_env.facts)).outcome == "env_error"
    blocked = base_side_outcome([step("s1", "blocked_by_gateway")], None, "gadget.new", led_to=True)
    assert classify(Facts(**blocked.facts)).reason == "step_failed:s1"


def test_both_builds_failing_a_step_is_a_flow_bug_labeled_both_sides():
    v = base_side_outcome([step("s1", "failed"), step("s1", "failed", "base")], manifest(NEW), "gadget.new")
    c = classify(Facts(**v.facts))
    assert (v.case, c.outcome, c.reason, c.side) == ("both", "not_run", "step_failed:s1", "both")
    # a base failure earlier than the candidate's is named, and the candidate rule decides
    v = base_side_outcome([step("s1", "failed", "base"), step("s1"), step("s2", "failed")], manifest(NEW), "gadget.new")
    assert v.case == "candidate" and v.facts["not_run"].reason == "step_failed:s2" and "s1" in v.facts["not_run"].detail
    assert base_side_outcome([step("s1"), step("s1", side="base")], None, "gadget.new").case == "passed"
    assert base_side_outcome([step("s1", "passed_after_retry", None)], None, "x").facts == {}


# -- 3.11 input B and the base re-capture (row 9) ---------------------------------------------------------------

def test_input_b_reasons():
    def b(**kw):
        new = kw.pop("new", False)
        r = b_input_reason(BaseEvidence(**kw), new_surface=new)
        return r.b, r.reason
    assert b(captured=True) == ("present", None)
    assert b(captured=True, failures=("not_settled",)) == ("present", None)      # the re-capture settled
    assert b(failures=("not_settled", "not_settled")) == ("absent", "base_not_settled")
    assert b(failures=("not_settled", "env_error")) == ("absent", "base_env_error")
    assert b(failures=("harness_error", "harness_error")) == ("absent", "base_harness_error")
    assert b(new=True) == ("absent", "new_surface")
    assert b(lineage_missing=True) == ("absent", "lineage_missing")
    assert b(reuse_missing=True) == ("absent", "reuse_missing")
    assert b(explorer_replay=True, captured=True) == ("absent", "explorer_replay")
    assert b(removed=True) == ("removed", None)
    assert b(step_failed="s3", new=True) == ("absent", "new_surface")
    r = b_input_reason(BaseEvidence(step_failed="s3"), new_surface=False)
    assert r.b is None and r.facts == {"not_run": r.not_run} and r.not_run.reason == "base_step_failed"
    with pytest.raises(ValueError):
        b()
    with pytest.raises(ValueError):
        BaseEvidence(failures=("timeout",))
    for reason in ("base_not_settled", "lineage_missing", "explorer_replay"):
        c = classify(Facts(base="absent", base_reason=reason))
        assert (c.outcome, c.reason) == ("needs_baseline", reason)
    assert classify(Facts(**b_input_reason(BaseEvidence(removed=True), new_surface=False).facts,
                          manifest="undeclared")).row == "26"


def _grabs(*seqs):
    it = iter(seqs)

    def grab():
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    return grab


STABLE = frames([IMG] * 3)
SHAKY = frames([synth.perturb(IMG, s, 20, 90) for s in range(4)])
SPEC = SettleSpec()


def test_a_base_that_ends_not_settled_is_recaptured_once():
    bc = settle_base(_grabs(SHAKY, STABLE), SPEC)
    assert bc.settled and bc.failures == ("not_settled",) and bc.attempts == 2
    assert np.array_equal(bc.result.capture, IMG)
    assert b_input_reason(bc.evidence(), new_surface=False).b == "present"
    twice = settle_base(_grabs(SHAKY, SHAKY), SPEC)
    assert not twice.settled and twice.failures == ("not_settled", "not_settled")
    c = classify(Facts(**b_input_reason(twice.evidence(), new_surface=False).facts))
    assert (c.outcome, c.reason, c.side) == ("needs_baseline", "base_not_settled", "base")
    env = settle_base(_grabs(CaptureFailure("env_error", "occluded"), CaptureFailure("harness_error")), SPEC)
    assert env.failures == ("env_error", "harness_error") and env.details == ("occluded", None)
    assert b_input_reason(env.evidence(), new_surface=False).reason == "base_harness_error"
    once = settle_base(_grabs(SHAKY), SPEC, recapturable=False)          # the session moved on (choice 9)
    assert once.failures == ("not_settled",)
    assert b_input_reason(once.evidence(), new_surface=False).reason == "base_not_settled"
    with pytest.raises(ValueError):
        CaptureFailure("not_settled")


# -- 3.10 targeted re-run ---------------------------------------------------------------------------------------

def test_recapture_plans():
    assert recapture_plan("assertion", data_mode="frozen") == recapture_plan("assertion", data_mode="masked_live")
    assert recapture_plan("assertion", data_mode="frozen").candidate == 0
    budget = RerunBudget(10)
    budget.used = budget.limit - 1
    p = recapture_plan("diff", data_mode="frozen", recapturable=False, budget=budget)
    assert (p.candidate, p.unknown_cause, budget.used) == (0, "not_recapturable", budget.limit - 1)
    assert recapture_plan("diff", data_mode="frozen", budget=budget).candidate == 2
    assert recapture_plan("invariant", data_mode="frozen", budget=budget).unknown_cause == "budget"
    assert recapture_plan("diff", data_mode="masked_live").base == 2
    assert recapture_plan("diff", data_mode="frozen").base == 0
    assert recapture_plan("diff", data_mode="frozen", base_session_open=True).base == 2
    assert recapture_plan("diff", data_mode="frozen", base_session_open=True, base_reused=True).base == 0
    assert (recapture_plan("invariant", data_mode="masked_live").candidate,
            recapture_plan("invariant", data_mode="masked_live").base) == (1, 0)


def differ(a, b):
    return not np.array_equal(a, b)


OTHER = synth.perturb(IMG, 7, 200, 90)


def test_a_not_settled_recapture_is_retried_and_then_stable():
    plan = recapture_plan("diff", data_mode="frozen")
    taken = take_recaptures(plan.candidate, _grabs(SHAKY, STABLE, STABLE), SPEC)
    assert [t.recapture.settled for t in taken] == [False, True, True]
    assert all(t.result is not None for t in taken) and taken[0].source is SHAKY
    st = rerun_stability(plan, IMG, taken, differ)
    assert (st.stability, st.recaptures, st.outcome) == ("stable", 3, None)


def test_two_not_settled_recaptures_are_unstable():
    plan = recapture_plan("diff", data_mode="frozen")
    taken = take_recaptures(plan.candidate, _grabs(STABLE, SHAKY, SHAKY), SPEC)
    assert [t.recapture.settled for t in taken] == [True, False, False]
    st = rerun_stability(plan, IMG, taken, differ)
    assert st.stability == "unstable" and st.outcome == ("not_settled", "unstable")
    assert len(take_recaptures(2, _grabs(SHAKY, SHAKY), SPEC)) == 2         # the series stops at the second


def test_masked_live_base_recaptures_are_compared_among_themselves():
    plan = recapture_plan("diff", data_mode="masked_live")
    cand = take_recaptures(plan.candidate, _grabs(STABLE, STABLE), SPEC)
    base = take_recaptures(plan.base, _grabs(frames([OTHER] * 3), STABLE), SPEC)
    st = rerun_stability(plan, IMG, cand, differ, base_first=IMG, base_taken=base)
    assert st.stability == "unstable" and ("base", 0, 1) in st.pairs_above
    steady = take_recaptures(plan.base, _grabs(STABLE, STABLE), SPEC)
    assert rerun_stability(plan, IMG, cand, differ, base_first=IMG, base_taken=steady).stability == "stable"
    shaky = take_recaptures(plan.base, _grabs(SHAKY, SHAKY), SPEC)
    assert rerun_stability(plan, IMG, cand, differ, base_first=IMG, base_taken=shaky).stability == "unstable"


def test_invariant_and_unknown_rerun_stability():
    plan = recapture_plan("invariant", data_mode="frozen")
    fired = take_recaptures(plan.candidate, _grabs(("again", STABLE)), SPEC, frames=lambda s: s[1],
                            fired=lambda s: s[0] == "again")
    assert fired[0].recapture.invariant_fired is True
    assert rerun_stability(plan, IMG, fired).stability == "stable"
    gone = take_recaptures(plan.candidate, _grabs(("gone", STABLE)), SPEC, frames=lambda s: s[1],
                           fired=lambda s: s[0] == "again")
    assert rerun_stability(plan, IMG, gone).stability == "unstable"
    no = recapture_plan("diff", data_mode="frozen", recapturable=False)
    st = rerun_stability(no, IMG, (), differ)
    assert (st.stability, st.cause) == ("unknown", "not_recapturable")
    with pytest.raises(ValueError):
        rerun_stability(recapture_plan("assertion", data_mode="frozen"), IMG, ())


def test_an_unstable_rerun_writes_one_flake_event(tmp_path):
    plan = recapture_plan("diff", data_mode="frozen")
    taken = take_recaptures(plan.candidate, _grabs(STABLE, frames([OTHER] * 3)), SPEC)
    st = rerun_stability(plan, IMG, taken, differ)
    assert st.stability == "unstable"
    row = flake_event(st, ["sha256:first", "sha256:r1", "sha256:r2"], region_id="rgn.ticket.send")
    assert row["metrics"]["pairs_above"] == [["candidate", 0, 2], ["candidate", 1, 2]]
    assert flake_event(rerun_stability(plan, IMG, take_recaptures(2, _grabs(STABLE, STABLE), SPEC), differ),
                       ["a", "b"]) is None
    with pytest.raises(ValueError):
        flake_event(st, ["sha256:first"])
    cid = f"census/gadget.one/{WS}"
    with Store(tmp_path / "run.db") as s:
        s.register_rig("rig-a1", login_alias="paper-01", owner="qa", snapshot_id="snap-1", allowlist_entry=1)
        s.begin_run(RunPlan(run_id="run-1", run_type="pr", config_commit="c0ffee", config_tarball_sha256="a" * 64,
                            snapshot_set_sha256="b" * 64, harness_sha="d00d", authorization_ref="auth/v1.md",
                            budget_sec=2700, scope=(ScopeEntry(cid, WS, "active_trader", 2, "frozen"),),
                            shards=(ShardPlan("run-1/1", "rig-a1"),), authorization_sha256="sha256:auth"))
        s.write_flake_event("run-1", cid, **row)
        rows = s.query("SELECT checkpoint_id, region_id, captures_json FROM flake_events")
    assert len(rows) == 1 and rows[0][0] == cid and rows[0][1] == "rgn.ticket.send"
    assert "sha256:first" in rows[0][2] and "sha256:r2" in rows[0][2]


# -- SD-21: any missing landmark refuses the whole checkpoint; the hash short-circuit comes first ----------------

def test_a_missing_landmark_refuses_every_panel_after_the_hash_short_circuit():
    wx, wy, _, _ = SCREEN.get("watchlist.title.label").bbox
    px, py, _, _ = SCREEN.get("positions.title.label").bbox
    reg = RegionsFile("census/gadget.one/" + WS, [
        Landmark("lm.watchlist", SCREEN.crop("watchlist.title.label", 2), expected=(wx - 2, wy - 2)),
        Landmark("lm.positions", SCREEN.crop("positions.title.label", 2), expected=(px - 2, py - 2))],
        [Region("rgn.watchlist.grid", "diff", "lm.watchlist", Rect(0, 20, 290, 140)),
         Region("rgn.positions.grid", "diff", "lm.positions", Rect(0, 20, 300, 140))])
    no_positions = copy.deepcopy(LAYOUT)
    synth.node(no_positions, "positions")["title"] = ""
    cand = synth.render(no_positions, SIZE).image
    ctx = DiffContext("run-1", reg.checkpoint_id, 3)                          # tier 3: no tier-1 qualifier
    res = diff_pair(IMG, cand, reg, context=ctx)
    assert res.terminal is not None and (res.terminal.outcome, res.terminal.reason, res.terminal.side) == (
        "landmark_missing", "landmark:lm.positions", "candidate")
    assert res.level is None and not res.regions              # no panel is diffed, not even the one found
    same = diff_pair(cand, cand.copy(), reg, context=ctx)    # hash-equal: zero before the landmark check
    assert same.terminal is None and same.level == "zero"
    c = classify(Facts(landmark=Condition(res.terminal.reason, side=res.terminal.side)))
    assert (c.outcome, c.reason) == ("landmark_missing", "landmark:lm.positions")


def test_a_regions_file_landmark_missing_on_either_side_settles_and_reaches_the_diff(tmp_path):
    # capture-eval:0, SD-37, 4.5, 5.7 Inputs: settle's must-see anchors are the registry landmarks with
    # `must_see: true`; the regions file gives settle only its masks. A regions-file landmark missing on a build is
    # 3.7's landmark_missing (landmark:<id>) with the side (the PR comment's Unexpected list), not not_settled.
    wx, wy, _, _ = SCREEN.get("watchlist.title.label").bbox
    px, py, _, _ = SCREEN.get("positions.title.label").bbox
    clock = Region("rgn.title.clock", "mask", "lm.watchlist", Rect(CLOCK.x - wx + 2, CLOCK.y - wy + 2, 70, 16))
    reg = RegionsFile("census/gadget.one/" + WS, [
        Landmark("lm.watchlist", SCREEN.crop("watchlist.title.label", 2), expected=(wx - 2, wy - 2)),
        Landmark("lm.positions", SCREEN.crop("positions.title.label", 2), confidence_min=0.5,
                 expected=(px - 2, py - 2))],
        [clock, Region("rgn.positions.grid", "diff", "lm.positions", Rect(0, 20, 300, 140))])
    cfg = config(tmp_path, [surface()])                                     # no must_see registry landmark
    spec = settle_spec_for(cfg, checkpoint(), regfile=reg)
    assert spec.anchors == [] and list(spec.ignore) == [CLOCK]              # the masks still come from the file
    no_positions = copy.deepcopy(LAYOUT)
    synth.node(no_positions, "positions")["title"] = ""
    cand = synth.render(no_positions, SIZE).image
    assert settle(frames([cand] * 4), spec).settled and settle(frames([IMG] * 4), spec).settled
    for base, candidate, side in ((IMG, cand, "candidate"), (cand, IMG, "base")):
        res = diff_pair(base, candidate, reg, context=DiffContext("run-1", reg.checkpoint_id, 1))
        assert (res.terminal.outcome, res.terminal.reason, res.terminal.side) == (
            "landmark_missing", "landmark:lm.positions", side)
