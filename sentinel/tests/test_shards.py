"""The shard planner (sentinel.shards): groups by shard key, LPT packing with session startups and workspace switches,
masked_live pairs first, frozen reuse, and the A/A mode (11.3, 2.11 order_and_cut, 11.2, SD-14)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from sentinel.shards import (FIXTURE_RESET_STARTUP_S, SESSION_STARTUP_S, WORKSPACE_SWITCH_S, Startups, plan,
                             rig_pairs)


@dataclass(frozen=True)
class Cp:
    checkpoint_id: str
    workspace_id: str = "ws.a"
    data_mode: str = "frozen"


@dataclass
class It:
    item_id: str
    shard_key: str
    estimated_s: float
    data_mode: str = "frozen"
    workspace: str = "ws.a"
    kind: str = "census"
    tier: int = 1
    rigs: tuple[str, ...] = ()
    checkpoints: list = field(default_factory=list)

    def __post_init__(self):
        self.checkpoints = [Cp(f"census/x.{self.item_id}/{self.workspace}", self.workspace, self.data_mode)]


@dataclass(frozen=True)
class R:
    id: str
    fingerprint_id: str = "fp"


RIGS = [R(f"rig-{i}") for i in range(1, 5)]


def test_items_of_one_key_share_one_rig_and_shard():
    items = [It(f"m{i}", "census:monitor:frozen", 12) for i in range(30)] + [It(f"t{i}", "census:trade:frozen", 12)
                                                                              for i in range(10)]
    layout = plan(items, RIGS)
    assert len({it.rigs for it in items if it.shard_key == "census:monitor:frozen"}) == 1
    assert len({it.rigs for it in items if it.shard_key == "census:trade:frozen"}) == 1
    assert {it.rigs for it in items[:1]} != {it.rigs for it in items[30:31]}       # two groups, two rigs
    shards = layout.shards()
    assert sorted(len(s.items) for s in shards) == [10, 30] and all(s.data_mode == "frozen" for s in shards)


def test_lpt_balances_rig_loads_within_one_group():
    sizes = [700, 650, 600, 420, 400, 380, 300, 240, 200, 120, 90, 60]
    items = [It(f"g{i}", f"flow:1:g{i}", s, kind="flow") for i, s in enumerate(sizes)]
    layout = plan(items, RIGS)
    loads = [layout.load_s(r.id) for r in RIGS]
    assert max(loads) - min(loads) <= 2 * max(sizes)          # each frozen item runs base then candidate
    assert all(layout.cost(r.id).sessions == 2 for r in RIGS)


def test_startups_8_min_for_a_frozen_shard_without_reuse_4_min_otherwise():
    one = [It("a", "k", 100)]
    layout = plan(one, [R("rig-1")])
    cost = layout.cost("rig-1")
    assert (cost.capture_s, cost.sessions, cost.startup_s) == (200, 2, 2 * SESSION_STARTUP_S)   # base, then candidate
    reused = plan([It("a", "k", 100)], [R("rig-1")], reuse=lambda cid, fp: True).cost("rig-1")
    assert (reused.capture_s, reused.sessions, reused.startup_s) == (100, 1, SESSION_STARTUP_S)
    other_fp = plan([It("a", "k", 100)], [R("rig-1", "fp-b")], reuse=lambda cid, fp: fp == "fp").cost("rig-1")
    assert other_fp.capture_s == 200                       # reuse is decided under the rig's own fingerprint (3.1)
    reset = plan([It("a", "k", 100)], [R("rig-1")], startups=Startups(fixture_reset=True)).cost("rig-1")
    assert reset.startup_s == 2 * FIXTURE_RESET_STARTUP_S
    assert layout.sessions("rig-1") == [False, False]


def test_workspace_switches_cost_45_s_per_session():
    items = [It("a", "k", 10, workspace="ws.a"), It("b", "k", 10, workspace="ws.b"), It("c", "k", 10, workspace="ws.c")]
    cost = plan(items, [R("rig-1")]).cost("rig-1")
    assert cost.switch_s == 2 * 2 * WORKSPACE_SWITCH_S       # two switches in the base session and two in the candidate
    assert cost.total_s == cost.capture_s + cost.startup_s + cost.switch_s and cost.work_s == cost.capture_s + cost.switch_s


def test_masked_live_goes_to_pairs_first_and_frozen_fills_idle_pair_rigs():
    assert rig_pairs([R("r1", "a"), R("r2", "b"), R("r3", "a"), R("r4", "a"), R("r5", "a")]) == [("r1", "r3"), ("r4", "r5")]
    live = [It("live", "census:monitor:masked_live", 300, data_mode="masked_live")]
    frozen = [It(f"f{s}", f"flow:1:f{s}", s, kind="flow") for s in (400, 300, 200, 100)]
    layout = plan(live + frozen, RIGS)
    assert live[0].rigs == ("rig-1", "rig-2")
    for rid in live[0].rigs:      # per-side time and one session on each rig of the pair, beside its frozen base+candidate
        assert layout.cost(rid).sessions == 1 + 2
    # longest first onto the least loaded rig: the rigs that remain first, then the pair rigs once they are the idlest
    assert [it.rigs for it in frozen] == [("rig-3",), ("rig-4",), ("rig-1",), ("rig-2",)]
    masked = [s for s in layout.shards() if s.data_mode == "masked_live"]
    assert len(masked) == 1 and masked[0].rigs == ("rig-1", "rig-2") and masked[0].sessions == 1


def test_masked_live_without_a_pair_is_left_unplaced():
    live = It("live", "k", 30, data_mode="masked_live")
    layout = plan([live, It("f", "k2", 30)], [R("rig-1", "a"), R("rig-2", "b")])
    assert layout.unplaced == [live] and live.rigs == ()


def test_remove_recomputes_the_rig_load_and_sessions():
    a, b = It("a", "k", 100), It("b", "k", 50, workspace="ws.b")
    layout = plan([a, b], [R("rig-1")], reuse=lambda cid, fp: "x.b" in cid)
    before = layout.cost("rig-1")
    assert (before.capture_s, before.sessions) == (250, 2)
    layout.remove(a)                                          # the only item that needed a base session
    after = layout.cost("rig-1")
    assert (after.capture_s, after.sessions, after.switch_s) == (50, 1, 0) and layout.items_on("rig-1") == [b]


def test_aa_mode_runs_every_item_passes_times_on_one_rig_in_one_session():
    items = [It("f", "k1", 10), It("m", "k2", 10, data_mode="masked_live")]
    layout = plan(items, [R("rig-4"), R("rig-5")], mode="aa", passes=2, seed_s={"rig-5": 50 * 60})
    assert all(len(it.rigs) == 1 for it in items) and {it.rigs for it in items} == {("rig-4",)}
    cost = layout.cost("rig-4")
    assert (cost.capture_s, cost.sessions) == (40, 1)
    assert [s.data_mode for s in layout.shards()] == ["any"]
    with pytest.raises(ValueError):
        plan(items, RIGS, mode="lockstep")


def test_plans_are_deterministic():
    def run():
        items = [It(f"g{i}", f"k{i % 7}", 10 + (i * 37) % 50) for i in range(60)]
        plan(items, RIGS)
        return [(it.item_id, it.rigs) for it in items]
    assert run() == run()
