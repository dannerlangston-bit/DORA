"""The run's Reviewer queue: cap order by tier then component area (3.15), the global-shift stop (3.7, 3.11 notes,
10.6) and the Nightly aa_drift_global trip (10.6)."""
import numpy as np
import pytest

from sentinel.classify import Facts, Review, classify
from sentinel.diff.engine import DiffResult, GlobalShiftSignal, Terminal, global_shift_prepass
from sentinel.records import CheckpointOutcome, Component, DiffSummary, Finding
from sentinel.report.cluster import Cluster, Member, cluster_members
from sentinel.review_queue import (
    QUEUE_STOPPED, admit, cluster_area, cluster_tier, global_shift_stop, nightly_global_shift, order, rank,
)

RUN = "run-1"
WS = "ws.tier-a.active-trader"


def cid(surface):
    return f"census/{surface}/{WS}"


def member(surface, tier, areas, *, region="rgn.a", phash="0" * 16, decision=None, outcome="needs_human",
           reason="reviewer_unavailable", review_state="pending"):
    comps = [Component(a, (10 * i, 10, 4, 4), region) for i, a in enumerate(areas)]
    o = CheckpointOutcome(RUN, cid(surface), outcome, WS, "active_trader", "frozen", reason=reason, tier=tier,
                          review_state=review_state, diff=DiffSummary(components=comps))
    f = Finding("f", RUN, "collateral", "", RUN, checkpoint_id=o.checkpoint_id, region_id=region, phash=phash,
                category=outcome)
    return Member(o, f, outcome, 0, region, decision)


# -- 3.15 cap order ---------------------------------------------------------------------------------------------

def test_clusters_rank_by_tier_then_component_area_then_checkpoint_id():
    t3 = Cluster([member("a.tier-three", 3, [500])])
    t1_small = Cluster([member("z.tier-one", 1, [10])])
    t1_big = Cluster([member("y.tier-one", 1, [60]), member("y.tier-two", 2, [50])])
    t1_tie = Cluster([member("x.tier-one", 1, [10])])
    no_tier = Cluster([member("b.no-tier", None, [900])])
    assert (cluster_tier(t1_big), cluster_area(t1_big)) == (1, 110)
    assert rank(t1_big) == (1, -110, cid("y.tier-one"))
    assert order([t3, no_tier, t1_small, t1_tie, t1_big]) == [t1_big, t1_tie, t1_small, t3, no_tier]


def test_suppressed_members_do_not_rank_a_cluster_with_unsuppressed_ones():
    mixed = Cluster([member("a.one", 1, [900], decision="dec-1"), member("a.two", 3, [5])])
    assert (cluster_tier(mixed), cluster_area(mixed)) == (3, 5)
    only = Cluster([member("a.one", 1, [900], decision="dec-1")])
    assert (cluster_tier(only), cluster_area(only)) == (1, 900)


def test_the_cap_reviews_tier_one_first_and_caps_the_rest():
    """03-review-cap-ranking: cap 1, a tier-3 cluster with the smaller checkpoint id and a tier-1 one."""
    t3 = member("a.tier-three", 3, [500], phash="f" * 16)
    t1 = member("z.tier-one", 1, [20])
    clusters = cluster_members([t3, t1])
    assert clusters[0].members[0] is t3                            # the report order is by checkpoint id
    adm = admit(clusters, 1, lambda c: True)
    assert [c.members[0] for c in adm.reviewed] == [t1] and [c.members[0] for c in adm.capped] == [t3]
    capped = classify(Facts(diff="above", stability="stable", manifest="declared",
                            review=Review("capped", cause="call_cap")))
    assert (capped.outcome, capped.reason, capped.review_state) == ("needs_human", "reviewer_cap", "none")
    # a cluster that needs no call takes no place under the cap
    adm = admit(clusters, 1, lambda c: c.members[0] is t3)
    assert ([c.members[0] for c in adm.reviewed], adm.capped, [c.members[0] for c in adm.skipped]) == ([t3], (), [t1])
    assert admit(clusters, 0, lambda c: True).reviewed == ()
    with pytest.raises(ValueError):
        admit(clusters, -1, lambda c: True)


# -- 3.7 granularity 0: the stopped queue ------------------------------------------------------------------------

def signal(name, changed=0, edge=0, text_edge_only=True):
    above = changed > 0
    return GlobalShiftSignal(cid(name), "above" if above else "zero", above, changed, edge, text_edge_only)


SIGNALS = ([signal("a.declared", 100, 95), signal("b.undeclared", 100, 95),
            signal("c.declared-mixed", 100, 80, text_edge_only=False)]
           + [signal(f"quiet.s{i:02d}") for i in range(22)])
ABOVE = dict(diff="above", stability="stable", threshold_source="calibrated")


def run_facts():
    return {
        cid("a.declared"): Facts(**ABOVE, manifest="declared"),
        cid("b.undeclared"): Facts(**ABOVE, manifest="undeclared"),
        cid("c.declared-mixed"): Facts(**ABOVE, manifest="declared"),
        cid("d.below"): Facts(diff="below", manifest="declared"),
        cid("e.removed"): Facts(base="removed", manifest="declared"),
    }


def test_a_global_shift_stops_the_reviewer_queue():
    shift = global_shift_prepass(SIGNALS)
    assert shift.raised and shift.collapse == (cid("a.declared"), cid("b.undeclared"))
    facts = run_facts()
    before = {k: classify(f) for k, f in facts.items()}
    assert before[cid("a.declared")].review_state == "pending"          # row 23: would have been queued
    stop = global_shift_stop(shift, facts)
    assert stop.stopped and stop.collapsed == shift.collapse
    a, b, c, e = (stop.classified[cid(s)] for s in ("a.declared", "b.undeclared", "c.declared-mixed", "e.removed"))
    assert (a.outcome, a.reason, a.exception, a.review_state) == ("needs_human", "global_shift", "global_shift", "done")
    assert (b.outcome, b.reason, b.exception, b.review_state) == ("needs_human", "global_shift", "global_shift", "none")
    assert stop.facts[cid("b.undeclared")].review is None              # row 16 reads no verdict
    assert (c.outcome, c.reason, c.review_state, c.exception) == ("needs_human", "reviewer_unavailable", "done", None)
    assert QUEUE_STOPPED in c.detail
    assert (e.row, e.reason, e.review_state) == ("25a", "reviewer_unavailable", "done")
    assert cid("d.below") not in stop.facts                           # untouched: no verdict, no shift
    # nothing is left awaiting a verdict, so the run makes zero Reviewer calls (10.6)
    final = {k: stop.classified.get(k, before[k]) for k in facts}
    assert all(cl.inputs.R != "pending" for cl in final.values() if cl.row in ("20", "21", "22", "23", "24", "25"))
    assert all(cl.review_state != "pending" for cl in final.values())
    # the collapsed checkpoints form one report cluster
    members = []
    for k, cl in final.items():
        if cl.outcome == "needs_human":
            o = CheckpointOutcome(RUN, k, "needs_human", WS, "active_trader", "frozen", **{
                n: v for n, v in cl.fields().items() if n not in ("outcome",)})
            members.append(Member(o, None, "needs_human", 0, None))
    shifted = [cl for cl in cluster_members(members) if cl.members[0].reason_code == "global_shift"]
    assert len(shifted) == 1 and sorted(m.checkpoint_id for m in shifted[0].members) == list(shift.collapse)


def test_no_shift_leaves_the_queue_running():
    quiet = global_shift_prepass([signal(f"quiet.s{i:02d}") for i in range(25)])
    assert not global_shift_stop(quiet, run_facts()).stopped
    few = global_shift_prepass(SIGNALS[:10])                           # under 20 pairs: not evaluated
    assert not few.evaluated and global_shift_stop(few, run_facts()).facts == {}
    with pytest.raises(ValueError, match="nightly"):
        global_shift_stop(global_shift_prepass(SIGNALS, nightly=True), run_facts())


# -- 10.6 the Nightly A/A form -----------------------------------------------------------------------------------

def pair(name, changed=0, edge=0, source="calibrated", synthetic=False, terminal=None):
    s = signal(name, changed, edge, edge == changed)
    s = GlobalShiftSignal(s.checkpoint_id, s.level, s.above_floor, s.changed_px, s.text_edge_px, s.text_edge_only,
                          synthetic)
    return cid(name), DiffResult(None if terminal else s.level, source, DiffSummary(), [],
                                 np.zeros((1, 1, 3), np.uint8), signal=None if terminal else s, synthetic=synthetic,
                                 terminal=terminal)


def pairs(*extra, quiet=24):
    return dict([pair(f"quiet.s{i:02d}") for i in range(quiet)] + list(extra))


def test_more_than_two_percent_above_floor_is_aa_drift_global():
    lost = pair("lost", terminal=Terminal("landmark_missing", "landmark:lm.x", side="candidate"))
    n = nightly_global_shift(pairs(pair("moved", 100, 10), lost))
    assert (n.terminal.outcome, n.terminal.reason) == ("harness_error", "aa_drift_global")
    assert not n.prepass.raised                                        # the 2 percent rule alone trips it
    assert len(n.applies_to) == 25 and cid("lost") not in n.applies_to
    small = nightly_global_shift(pairs(pair("moved", 100, 10), quiet=9))   # under 20 pairs, still 10 percent
    assert small.terminal is not None and not small.prepass.evaluated


def test_the_prepass_alone_trips_aa_drift_global():
    n = nightly_global_shift(pairs(pair("glyphs", 100, 95, source="uncalibrated")))
    assert n.drift.global_drift is None and n.prepass.raised
    assert n.terminal.reason == "aa_drift_global" and len(n.applies_to) == 25


def test_a_quiet_night_and_synthetic_pairs_trip_nothing():
    n = nightly_global_shift(pairs(*[pair(f"mutant.m{i}", 100, 95, synthetic=True) for i in range(3)]))
    assert n.terminal is None and n.applies_to == () and n.drift.above_floor_fraction == 0.0
