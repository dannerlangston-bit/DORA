"""Path equivalence: one real merge-run pair re-evaluated through the canary entry point (10.3, 5.15, SD-81)."""
from __future__ import annotations

import dataclasses

import pytest

from sentinel import canary as C
from sentinel import canary_paths as P
from sentinel.records import CheckpointOutcome, Component, DiffSummary, RegionDiff
from sentinel.store import RunPlan, ScopeEntry, Store
from sentinel.store.artifacts import ArtifactStore, Redaction

MERGE = "run-2026-11-10-merge-0001"
NIGHTLY = "run-2026-11-11-nightly-0001"
WS = "ws.tier-a.active-trader"
PAIRS = ("census/monitor.activity.position-statement/ws.tier-a.active-trader",
         "census/sidebar.account-info/ws.tier-a.active-trader",
         "census/trade.all-products.option-chain/ws.tier-a.active-trader")
NO_CAPTURE = "census/monitor.activity.working-orders/ws.tier-a.active-trader"


def plan(run_id: str, run_type: str, harness: str = "d00d") -> RunPlan:
    return RunPlan(run_id=run_id, run_type=run_type, config_commit="c0ffee", config_tarball_sha256="a" * 64,
                   snapshot_set_sha256="b" * 64, harness_sha=harness, authorization_ref="auth/v1.md", budget_sec=10800,
                   scope=tuple(ScopeEntry(c, WS, "active_trader", 1, "frozen") for c in PAIRS + (NO_CAPTURE,)))


def diff(k: int) -> DiffSummary:
    return DiffSummary(whole_pixel_fraction=0.01 * k, whole_ssim=0.99 - 0.01 * k, shift=(0, 0),
                       regions=[RegionDiff("rgn.grid", 0.02 * k, 0.97, True), RegionDiff("rgn.header", 0.0, 1.0, False)],
                       components=[Component(40 * k, (10, 10 * k, 5, 8), "rgn.grid", (2, 2, 5, 8)),
                                   Component(9, (30, 4, 3, 3), "rgn.header", (1, 1, 3, 3))])


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "sentinel.db")
    s.begin_run(plan(MERGE, "merge"))
    arts = ArtifactStore(tmp_path / "artifacts", f"runs/{MERGE}")
    for k, cid in enumerate(PAIRS, 1):
        base = arts.put(f"base {cid}".encode(), name=f"{k}/base.png", kind="base", media_type="image/png",
                        redaction=Redaction("rv-1"))
        cand = arts.put(f"candidate {cid}".encode(), name=f"{k}/candidate.png", kind="candidate",
                        media_type="image/png", redaction=Redaction("rv-1"))
        s.write_checkpoint(CheckpointOutcome(run_id=MERGE, checkpoint_id=cid, outcome="collateral", workspace_id=WS,
                                             persona="active_trader", data_mode="frozen", tier=1, diff=diff(k),
                                             artifacts={"base": base.sha256, "candidate": cand.sha256}),
                           artifacts=[base, cand])
    s.write_checkpoint(CheckpointOutcome(run_id=MERGE, checkpoint_id=NO_CAPTURE, outcome="env_error",
                                         gate="launched", reason="timeout", workspace_id=WS,
                                         persona="active_trader", data_mode="frozen", tier=1))
    s.begin_run(plan(NIGHTLY, "nightly"))
    yield s
    s.close()


def same(stored: CheckpointOutcome) -> CheckpointOutcome:
    """The canary entry point on an unchanged engine: the same record, flagged synthetic."""
    return dataclasses.replace(stored, run_id=NIGHTLY, synthetic=True)


def test_an_unchanged_engine_reproduces_the_pair(store):
    got = P.path_equivalence(store, MERGE, C.SEED, same, run_id=NIGHTLY, catalog_version="cat-v1")
    assert got is not None and not got.diverged and got.outcome_row is None
    assert got.stored.checkpoint_id in PAIRS and got.stored.synthetic is False
    r = got.row
    assert (r.kind, r.defect_class, r.mutant_id, r.money_cell, r.state) == \
        ("path_equivalence", "path_equivalence", MERGE, True, "detected")
    assert (r.base_sha256, r.mutant_sha256) == (got.stored.artifacts["base"], got.stored.artifacts["candidate"])
    assert r.expected == ["collateral"] and r.produced_outcome == "collateral" and r.absorbed_by is None


def test_the_pair_is_chosen_by_seed_among_captured_real_rows(store):
    eligible = P.eligible_pairs(store, MERGE)
    assert [o.checkpoint_id for o in eligible] == sorted(PAIRS)                   # the env_error row has no captures
    picks = {seed: P.choose_pair(eligible, seed).checkpoint_id for seed in range(12)}
    assert len(set(picks.values())) > 1
    assert all(P.choose_pair(eligible, s).checkpoint_id == c for s, c in picks.items())   # stable per seed
    assert P.path_equivalence(store, "run-2026-11-10-merge-0099", 1, same, run_id=NIGHTLY, catalog_version="v") is None


def test_a_perturbed_engine_parameter_diverges(store):
    """A diff parameter changed only on the canary entry: the per-region metrics differ."""
    def perturbed(stored: CheckpointOutcome) -> CheckpointOutcome:
        d = dataclasses.replace(stored.diff, regions=[dataclasses.replace(stored.diff.regions[0], pixel_fraction=0.5),
                                                      stored.diff.regions[1]])
        return dataclasses.replace(same(stored), diff=d)
    got = P.path_equivalence(store, MERGE, C.SEED, perturbed, run_id=NIGHTLY, catalog_version="cat-v1",
                             watchdog_event="wd-41")
    assert got.diverged and got.row.state == "detector_blind"
    assert len(got.differences) == 1 and got.differences[0].startswith("region rgn.grid metrics (")
    o = got.outcome_row
    assert (o.run_id, o.checkpoint_id, o.outcome, o.reason, o.synthetic, o.mutant_id) == \
        (NIGHTLY, got.stored.checkpoint_id, "harness_error", "canary_path_divergence", False, MERGE)
    assert o.detail.startswith(f"watchdog_event: wd-41; merge run {MERGE} pair re-evaluated")
    store.write_checkpoint(o)                                                      # a real row of the Nightly
    assert store.get_outcome(NIGHTLY, o.checkpoint_id).reason == "canary_path_divergence"
    m09 = C.Mutant(entry=C.MutantEntry("M09", PAIRS[0], "shifted_panel", "image", False, "translate", {},
                                       C.Expected(("collateral",)), "sentinel-canaries", region_ref="rgn.grid",
                                       catalog_version="cat-v1"),
                   checkpoint_id=PAIRS[0], seed=C.SEED, base_sha256="b" * 64, mutant_sha256="m" * 64)
    green = C.score([C.MutantResult(m09, CheckpointOutcome(run_id=NIGHTLY, checkpoint_id=PAIRS[0], outcome="collateral",
                                                            workspace_id=WS, persona="active_trader",
                                                            data_mode="frozen", synthetic=True))])
    assert green.state == "ok"
    ds = C.persist(store, NIGHTLY, green, since_at="2026-11-11T08:12:00+00:00", since_text="2026-11-11 03:12 ET",
                   date="2026-11-11", extra_rows=[got.row])
    assert ds.state == "detector_blind" and ds.divergence and "canary_path_divergence" in ds.banner


def test_outcome_and_component_changes_are_named(store):
    def flipped(stored: CheckpointOutcome) -> CheckpointOutcome:
        comps = [dataclasses.replace(stored.diff.components[0], bbox=(11, 10, 5, 8)), stored.diff.components[1]]
        return dataclasses.replace(same(stored), outcome="needs_human", reason="reviewer_uncertain",
                                   diff=dataclasses.replace(stored.diff, components=comps, whole_ssim=0.5))
    got = P.path_equivalence(store, MERGE, C.SEED, flipped, run_id=NIGHTLY, catalog_version="cat-v1")
    text = "\n".join(got.differences)
    assert "outcome collateral re-evaluated as needs_human (reviewer_uncertain)" in text
    assert "whole-window metrics" in text and "components differ: 2 stored, 2 re-evaluated, same count" in text
    assert got.row.produced_reason == "reviewer_uncertain" and got.row.absorbed_by == "; ".join(got.differences)


def test_a_different_harness_commit_is_a_divergence(tmp_path):
    with Store(tmp_path / "sentinel.db") as s:
        s.begin_run(plan(MERGE, "merge"))
        arts = ArtifactStore(tmp_path / "artifacts", f"runs/{MERGE}")
        base = arts.put(b"b", name="base.png", kind="base", media_type="image/png", redaction=Redaction("rv-1"))
        cand = arts.put(b"c", name="cand.png", kind="candidate", media_type="image/png", redaction=Redaction("rv-1"))
        s.write_checkpoint(CheckpointOutcome(run_id=MERGE, checkpoint_id=PAIRS[0], outcome="collateral",
                                             workspace_id=WS, persona="active_trader", data_mode="frozen", tier=1,
                                             diff=diff(1), artifacts={"base": base.sha256, "candidate": cand.sha256}),
                           artifacts=[base, cand])
        s.begin_run(plan(NIGHTLY, "nightly", harness="beef"))
        got = P.path_equivalence(s, MERGE, 3, same, run_id=NIGHTLY, catalog_version="v")
        assert got.differences == ("harness commit beef is not the merge run's d00d (5.15)",)


def test_the_re_evaluation_must_use_the_canary_entry_point(store):
    with pytest.raises(ValueError, match="canary entry point"):
        P.path_equivalence(store, MERGE, C.SEED, lambda o: dataclasses.replace(o, run_id=NIGHTLY), run_id=NIGHTLY,
                           catalog_version="v")
