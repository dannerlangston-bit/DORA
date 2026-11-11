"""Path equivalence: the nightly re-evaluation of one real merge-run pair through the canary entry point (10.3 Path
equivalence, 5.15 Canary Service; 8.3 `canary_results` kind `path_equivalence`, SD-81).

Mutants enter through the same `evaluate_checkpoint` entry point and harness commit as the day's merge run; a
detector proven on a different path proves nothing, so once a night `path_equivalence(store, merge_run_id, seed,
evaluate)` picks one real checkpoint pair of the merge run by seed, has the caller's `evaluate` re-run it through
the canary entry point (`synthetic: true`) on the pair's stored base and candidate captures, and compares the
outcome, the metrics and the components with the stored product row, exactly. A difference is
`harness_error (reason: canary_path_divergence)` on the pair's checkpoint, with the detector-blind banner until a
later night reproduces (`canary.persist` raises the detector state from the returned row) and a P1 page
(`canary_integrations.alert_route("canary_path_divergence")`).

Choices where the spec is ambiguous (reported):

1. Eligible pairs: the merge run's real (non-synthetic) outcome rows that carry a diff and both capture artifacts
   (`base`, `candidate`), in checkpoint order; the pair is the one at `numpy.random.default_rng(seed)`'s first
   integer below their count, so the choice is stable for a seed and a merge run. A merge run with no eligible pair
   gives None: nothing could be re-evaluated, and the caller says so in run health.
2. "Outcome, metrics and components exactly" (10.3; "per-region metrics and component list", 5.15): the outcome and
   its reason; the whole-window pixel fraction, SSIM and shift; per region, by id, the pixel fraction, SSIM and
   above-threshold flag; the components as a multiset of (area, window bbox, region id, region-relative bbox).
   Floats compare exactly; tuples and lists compare alike, since the stored row has made a JSON round trip.
3. The harness commit (5.15: "the harness commit of the Nightly run equals the day's merge run's"): when both runs
   are in the store with different `harness_sha`, that is a difference too.
4. The `canary_results` row: kind and `defect_class` `path_equivalence` (SD-81), `mutant_id` the merge run id (the
   item), the pair's checkpoint, `money_cell` true (so a miss is `detector_blind`, the blind banner of 10.3), the
   stored outcome as `expected`, the re-evaluated outcome and reason as produced, the stored base and candidate
   sha256 as `base_sha256` and `mutant_sha256`, and the differences in `absorbed_by`; state `detected` when the pair
   reproduces.
5. On a difference the Nightly also writes the real outcome row `harness_error (reason: canary_path_divergence)` on
   the pair's checkpoint (`outcomes` holds the harness_error rows a synthetic cause writes on the run's checkpoints,
   10.3, SD-91), named by the merge run in `mutant_id` and with the run's Watchdog event id in `detail` (4.13); the
   caller scopes the checkpoint and writes it with `Store.write_checkpoint`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from .canary import CanaryResult
from .records import CheckpointOutcome, DiffSummary

if TYPE_CHECKING:
    from .store.store import Store

Evaluate = Callable[[CheckpointOutcome], CheckpointOutcome]


@dataclass(frozen=True)
class PathEquivalence:
    """One night's path-equivalence check: the stored pair, its re-evaluation and what differs."""
    stored: CheckpointOutcome                   # the merge run's product row
    produced: CheckpointOutcome                 # the same pair through the canary entry point
    differences: tuple[str, ...]
    row: CanaryResult                           # the `canary_results` row (kind path_equivalence)
    outcome_row: CheckpointOutcome | None       # harness_error (canary_path_divergence) for the Nightly, on a mismatch

    @property
    def diverged(self) -> bool:
        return bool(self.differences)


def eligible_pairs(store: Store, merge_run_id: str) -> list[CheckpointOutcome]:
    """The merge run's real checkpoint pairs that can be re-evaluated: a diff and both captures (choice 1)."""
    return [o for o in store.outcomes(merge_run_id)
            if not o.synthetic and o.diff is not None and (o.artifacts or {}).get("base")
            and (o.artifacts or {}).get("candidate")]


def choose_pair(pairs: list[CheckpointOutcome], seed: int) -> CheckpointOutcome:
    """The pair at the seed's index (choice 1)."""
    return pairs[int(np.random.default_rng(seed).integers(len(pairs)))]


def _box(b: Any) -> tuple[int, ...] | None:
    return None if b is None else tuple(int(v) for v in b)


def _metrics(d: DiffSummary) -> tuple[tuple[Any, ...], dict[str, tuple[Any, ...]], list[tuple[Any, ...]]]:
    whole = (d.whole_pixel_fraction, d.whole_ssim, _box(d.shift))
    regions = {r.region_id: (r.pixel_fraction, r.ssim, r.above_threshold) for r in d.regions}
    comps = sorted((c.area, _box(c.bbox), c.region_id or "", _box(getattr(c, "bbox_region", None)) or ())
                   for c in d.components)
    return whole, regions, comps


def _said(o: CheckpointOutcome) -> str:
    return o.outcome + (f" ({o.reason})" if o.reason else "")


def differences(stored: CheckpointOutcome, produced: CheckpointOutcome) -> list[str]:
    """What the re-evaluation did not reproduce, empty when the pair is equal (choice 2)."""
    out = []
    if (stored.outcome, stored.reason) != (produced.outcome, produced.reason):
        out.append(f"outcome {_said(stored)} re-evaluated as {_said(produced)}")
    if (stored.diff is None) != (produced.diff is None):
        return out + ["a diff on one side only"]
    if stored.diff is None or produced.diff is None:
        return out
    (wa, ra, ca), (wb, rb, cb) = _metrics(stored.diff), _metrics(produced.diff)
    if wa != wb:
        out.append(f"whole-window metrics {wa} re-evaluated as {wb}")
    out += [f"region {rid} metrics {ra.get(rid)} re-evaluated as {rb.get(rid)}"
            for rid in sorted(set(ra) | set(rb)) if ra.get(rid) != rb.get(rid)]
    if ca != cb:
        out.append(f"components differ: {len(ca)} stored, {len(cb)} re-evaluated"
                   + ("" if len(ca) != len(cb) else ", same count, different boxes or areas"))
    return out


def path_equivalence(store: Store, merge_run_id: str, seed: int, evaluate: Evaluate, *, run_id: str,
                     catalog_version: str, watchdog_event: str | None = None) -> PathEquivalence | None:
    """Re-evaluate one real pair of `merge_run_id` through the canary entry point and compare (10.3, 5.15).

    `evaluate` takes the stored product row and returns the record the canary entry point produces for the same
    base and candidate captures (`evaluate_checkpoint` with `synthetic: true`); `run_id` is the Nightly run, whose
    harness commit must equal the merge run's; `catalog_version` the night's catalog sha256 for the row;
    `watchdog_event` the run's Watchdog event id for the divergence row (4.13). None when the merge run has no
    eligible pair (choice 1).
    """
    pairs = eligible_pairs(store, merge_run_id)
    if not pairs:
        return None
    stored = choose_pair(pairs, seed)
    produced = evaluate(stored)
    if not produced.synthetic or produced.checkpoint_id != stored.checkpoint_id:
        raise ValueError(f"{stored.checkpoint_id}: the re-evaluation must come through the canary entry point "
                         "(synthetic: true) for the same checkpoint (10.3)")
    found = differences(stored, produced)
    shas = {r["run_id"]: r["harness_sha"] for r in
            store.query("SELECT run_id, harness_sha FROM runs WHERE run_id IN (?, ?)", (merge_run_id, run_id))}
    if len(shas) == 2 and shas[merge_run_id] != shas[run_id]:
        found.insert(0, f"harness commit {shas[run_id]} is not the merge run's {shas[merge_run_id]} (5.15)")
    arts = stored.artifacts or {}
    row = CanaryResult(
        mutant_id=merge_run_id, catalog_version=catalog_version, checkpoint_id=stored.checkpoint_id,
        defect_class="path_equivalence", money_cell=True, seed=seed, expected=[stored.outcome],
        state="detector_blind" if found else "detected", base_sha256=arts.get("base"),
        mutant_sha256=arts.get("candidate"), produced_outcome=produced.outcome, produced_reason=produced.reason,
        absorbed_by="; ".join(found) or None, kind="path_equivalence")
    outcome_row = None
    if found:
        detail = f"merge run {merge_run_id} pair re-evaluated through the canary entry point: {'; '.join(found)}"
        outcome_row = CheckpointOutcome(
            run_id=run_id, checkpoint_id=stored.checkpoint_id, outcome="harness_error",
            reason="canary_path_divergence", detail=f"watchdog_event: {watchdog_event}; {detail}" if watchdog_event
            else detail, workspace_id=stored.workspace_id, persona=stored.persona, data_mode=stored.data_mode,
            surface_id=stored.surface_id, test_kind=stored.test_kind, tier=stored.tier, flow_id=stored.flow_id,
            step_id=stored.step_id, size_step=stored.size_step, flow_class=stored.flow_class, mutant_id=merge_run_id)
    return PathEquivalence(stored, produced, tuple(found), row, outcome_row)
