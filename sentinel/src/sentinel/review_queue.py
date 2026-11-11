"""The run's Reviewer queue: cap order, the global-shift stop, and the Nightly variant of the pre-pass.

- `order(clusters)` ranks the report clusters of 9.7 for the per-run Reviewer call cap of 3.15 ("ranked by tier
  then component area so that tier-1 items are reviewed first"): the lowest tier among the members, then the
  summed changed-pixel area of their diff components, larger first, then the first checkpoint id. `admit` splits
  an ordered queue at the cap: the clusters that get a call, the ones beyond the cap (`needs_human (reason:
  reviewer_cap)`), and the ones that need no call.
- `global_shift_stop(shift, facts)` applies the granularity-0 pre-pass result of 3.7 to a product run: the text-edge
  checkpoints it collapsed become `needs_human (reason: global_shift)` (one report cluster, `report.cluster`), and
  the Reviewer queue of the run is stopped, so no item is sent (10.6: "with zero Reviewer calls").
- `nightly_global_shift(pairs)` is the Nightly A/A form (10.6): more than 2 percent of checkpoints above floor, or
  the pre-pass raised, is `harness_error (reason: aa_drift_global)`; both count only calibrated results (choice 3).

Choices where the spec is ambiguous (reported):

1. Cap order (3.15). A cluster is one queue item (one call, 3.15 Clustering). Its tier is the lowest member tier and
   its area the summed `area` (undilated changed pixels, 3.7 granularity 3) of every diff component of its members;
   only unsuppressed members count when there are any, since only they are reviewed. A member without a tier ranks
   after tier 3. Ties end on the first checkpoint id, so the order is total.
2. The stopped queue (3.7 "the Reviewer queue for the run is stopped"). The stop holds every item of the run, not
   only the collapsed ones: no Reviewer call of any task is made (no manifest match, no collateral description, no
   visibility check). An item whose row reads the verdict (3.11 rows 20 to 25a) and has none gets R `unavailable`
   with cause `reviewer_queue_stopped`: the queue will not drain for this run, so the item is final
   (`review_state: done`, as for a job that gave up, 3.11 notes) rather than `pending`. For a collapsed item the
   3.11 global-shift exception then gives `needs_human (reason: global_shift)` with that `review_state`; a declared
   item with a non-text-edge component is classified normally, `needs_human (reason: reviewer_unavailable)`, and
   enters the digest for human disposition. Items of rows 16 to 19 are untouched (they read no verdict).
3. Nightly (10.6). The run-level `harness_error (reason: aa_drift_global)` is the outcome of every compared A/A pair
   of the pass (`applies_to`): the thresholds are invalid until a Calibration run passes, so no pair is judged
   against them, and per-checkpoint `aa_drift` results under it do not count toward quarantine. Without it, each
   frozen checkpoint above its floor keeps its `not_settled (reason: aa_drift)` from `calibration.aa_drift`. The
   pre-pass reads "above floor" as `calibration.aa_drift` does: a checkpoint whose result is `uncalibrated` (no
   floor, an incomplete Calibration, a region added after it) has no floor to be above, so it stays `needs_human
   (reason: uncalibrated)` (10.6) and neither trips the global rule nor invalidates the floors that exist; it still
   counts in the denominator.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .classify import REVIEWER_ROWS, Classified, Facts, Review, classify
from .diff import load_params
from .diff.calibration import AADrift, aa_drift
from .diff.engine import DiffResult, GlobalShiftDecision, Terminal, global_shift_prepass
from .report.cluster import Cluster, Member

QUEUE_STOPPED = "reviewer_queue_stopped"     # the cause on R `unavailable` while the queue is stopped (choice 2)
NO_TIER = 4                                  # a member without a tier ranks after tier 3 (choice 1)


# --------------------------------------------------------------------------------------------
# 3.15 cap order
# --------------------------------------------------------------------------------------------

def _pool(cluster: Cluster) -> list[Member]:
    return cluster.unsuppressed or cluster.members


def cluster_tier(cluster: Cluster) -> int:
    """The lowest tier among the cluster's (unsuppressed) members."""
    return min((m.outcome.tier if m.outcome.tier is not None else NO_TIER) for m in _pool(cluster))


def cluster_area(cluster: Cluster) -> int:
    """The summed changed-pixel area of the diff components of the cluster's (unsuppressed) members."""
    return sum(c.area for m in _pool(cluster) if m.outcome.diff for c in m.outcome.diff.components)


def rank(cluster: Cluster) -> tuple[int, int, str]:
    """The 3.15 cap key: (lowest tier, minus summed component area, first checkpoint id)."""
    return cluster_tier(cluster), -cluster_area(cluster), min(m.checkpoint_id for m in cluster.members)


def order(clusters: Iterable[Cluster]) -> list[Cluster]:
    """The clusters in Reviewer queue order (3.15 Caps; choice 1)."""
    return sorted(clusters, key=rank)


@dataclass(frozen=True)
class Admission:
    """An ordered queue split at the cap: `reviewed` get a call, `capped` are `needs_human (reason: reviewer_cap)`
    and never queued, `skipped` need no Reviewer call."""

    reviewed: tuple[Cluster, ...]
    capped: tuple[Cluster, ...]
    skipped: tuple[Cluster, ...]


def admit(clusters: Iterable[Cluster], cap: int, needs_call: Callable[[Cluster], bool]) -> Admission:
    """Order the clusters and admit the first `cap` that need a call (3.15: PR 60, merge 200, RC 500)."""
    if cap < 0:
        raise ValueError("the reviewer call cap is not negative")
    reviewed: list[Cluster] = []
    capped: list[Cluster] = []
    skipped: list[Cluster] = []
    for cl in order(clusters):
        if not needs_call(cl):
            skipped.append(cl)
        elif len(reviewed) < cap:
            reviewed.append(cl)
        else:
            capped.append(cl)
    return Admission(tuple(reviewed), tuple(capped), tuple(skipped))


# --------------------------------------------------------------------------------------------
# 3.7 granularity 0 on a product run: the queue stops
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class QueueStop:
    """The pre-pass applied to the run's items. `stopped`: make no Reviewer call for the run. `facts` and
    `classified` hold every item whose facts changed (collapsed into global_shift, or given the stopped-queue R),
    by checkpoint id; `collapsed` lists the global_shift cluster's checkpoints."""

    stopped: bool
    collapsed: tuple[str, ...] = ()
    facts: Mapping[str, Facts] = field(default_factory=dict)
    classified: Mapping[str, Classified] = field(default_factory=dict)


def _awaits_verdict(f: Facts) -> bool:
    return f.review is None or f.review.state == "pending"


def global_shift_stop(shift: GlobalShiftDecision, facts: Mapping[str, Facts]) -> QueueStop:
    """Apply a product run's pre-pass (3.7, 3.11 notes; choice 2). `facts` maps every classified checkpoint of the
    run to its facts; call this after classification and before any Reviewer call. A Nightly decision (one with
    a `terminal`) is `nightly_global_shift`'s, never this function's."""
    if shift.terminal is not None:
        raise ValueError("a Nightly pre-pass gives harness_error (aa_drift_global); use nightly_global_shift")
    if not shift.raised:
        return QueueStop(False)
    collapse = set(shift.collapse)
    stopped = Review("unavailable", cause=QUEUE_STOPPED)
    out_f: dict[str, Facts] = {}
    out_c: dict[str, Classified] = {}
    for cid, f in facts.items():
        new = replace(f, global_shift=True) if cid in collapse and not f.global_shift else f
        if classify(new).row in REVIEWER_ROWS and _awaits_verdict(new):
            new = replace(new, review=stopped)
        if new is not f:
            out_f[cid], out_c[cid] = new, classify(new)
    return QueueStop(True, tuple(sorted(collapse & set(facts))), out_f, out_c)


# --------------------------------------------------------------------------------------------
# 10.6 the Nightly A/A form
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class NightlyShift:
    """The Nightly A/A verdict (10.6): `terminal` is `harness_error (reason: aa_drift_global)` or None,
    `applies_to` the checkpoints it is the outcome of (choice 3), `drift` the per-checkpoint `aa_drift` results
    and `prepass` the 3.7 pre-pass over the same pairs."""

    terminal: Terminal | None
    applies_to: tuple[str, ...]
    drift: AADrift
    prepass: GlobalShiftDecision


def nightly_global_shift(pairs: Mapping[str, DiffResult], *, frozen: Iterable[str] = (),
                         params: Mapping[str, Any] | None = None) -> NightlyShift:
    """10.6 over the Nightly A/A pairs (pass 1 against pass 2, by checkpoint id; `frozen` names the frozen
    checkpoints): more than 2 percent of checkpoints above floor, or the 3.7 pre-pass raised, is `harness_error
    (reason: aa_drift_global)` for every compared pair; only a calibrated result is above floor in either (choice
    3). Synthetic results (canary mutants) never count."""
    p = params or load_params()
    drift = aa_drift(pairs, frozen, p)
    real = {cid: r for cid, r in pairs.items() if not (r.synthetic or (r.signal is not None and r.signal.synthetic))}
    signals = [replace(r.signal, above_floor=r.signal.above_floor and r.threshold_source == "calibrated")
               for r in real.values() if r.signal is not None]                     # choice 3: aa_drift's rule
    prepass = global_shift_prepass(signals, nightly=True, params=p)
    terminal = drift.global_drift or prepass.terminal
    applies = tuple(sorted(cid for cid, r in real.items() if r.terminal is None)) if terminal is not None else ()
    return NightlyShift(terminal, applies, drift, prepass)

