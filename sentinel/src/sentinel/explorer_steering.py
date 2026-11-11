"""The Explorer's steering target list (2.9 Steering, 6.8).

Before the session starts the Orchestrator computes the list the Explorer is steered toward: surfaces with the
fewest flow checkpoints in the current RC run, and surfaces whose `primitives` intersect the manifest's touched
primitives. Only surfaces the Explorer can reach are listed: those its own saved workspace hosts (allowlisted tabs
only, 6.8) and none a deny-list entry covers whole. Odd inputs come from the seeded corpus (`odd_input_values`),
with the pass rule of 2.6.3. `steering_version` hashes the list, so the pause rule of `orchestrator.explorer` can
tell when the list was fixed.

Choices (reported):
1. The ranking is lexicographic: fewest flow checkpoints first, then surfaces on a touched primitive, then the
   higher tier (1 before 3), then the id. 2.9 names both criteria without weights; coverage comes first because the
   Explorer exists to look where the scripted flows do not.
2. A surface's flow checkpoints are the flow checkpoints of the RC run that targeted it and were exercised
   (an outcome other than `not_run`, or no outcome yet); a `not_run` checkpoint covers nothing.
3. The odd inputs are the corpus's nightly sample for the run's seed; their `odd_inputs.<id>` references join the
   Explorer's `typed_values`, so `type_fixture` types them by id and never as free text.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .canonical import sha256_id
from .oddinput import OddInputCorpus, sample
from .taxonomy import checkpoint_parts


@dataclass(frozen=True)
class SteeringTarget:
    """One entry of the target list: the surface, its flow checkpoints in this RC run, the touched primitives it
    hosts, its tier and its rank (1 first)."""

    surface_id: str
    flow_checkpoints: int
    touched: tuple[str, ...]
    tier: int
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {"surface_id": self.surface_id, "flow_checkpoints": self.flow_checkpoints,
                "touched": list(self.touched), "tier": self.tier, "rank": self.rank}


def flow_checkpoint_counts(outcomes: Iterable[tuple[str, str | None]]) -> Counter[str]:
    """Flow checkpoints per surface from the RC run's (checkpoint id, outcome or None) pairs (choice 2)."""
    out: Counter[str] = Counter()
    for cid, outcome in outcomes:
        parts = checkpoint_parts(cid)
        if parts["kind"] == "flow" and outcome != "not_run":
            out[parts["surface"]] += 1
    return out


def target_list(surfaces: Iterable[Mapping[str, Any]], flow_checkpoints: Mapping[str, int],
                touched_primitives: Iterable[str], *, reachable: Iterable[str] | None = None,
                denied_surfaces: Iterable[str] = ()) -> list[SteeringTarget]:
    """The ranked steering list (module docstring, choice 1). `surfaces` are registry surface documents (`id`,
    `tier`, `primitives`); `reachable` the surfaces the Explorer workspace hosts (None: every surface)."""
    touched = set(touched_primitives)
    allowed = None if reachable is None else set(reachable)
    denied = set(denied_surfaces)
    rows = []
    for s in surfaces:
        sid = s.get("id")
        if not isinstance(sid, str) or sid in denied or (allowed is not None and sid not in allowed):
            continue
        hit = tuple(sorted(touched & set(s.get("primitives") or ())))
        tier = s.get("tier") if s.get("tier") in (1, 2, 3) else 3
        rows.append((int(flow_checkpoints.get(sid, 0)), 0 if hit else 1, tier, sid, hit))
    rows.sort(key=lambda r: r[:4])
    return [SteeringTarget(sid, n, hit, tier, i) for i, (n, _, tier, sid, hit) in enumerate(rows, 1)]


def steering_version(targets: Iterable[SteeringTarget]) -> str:
    """`sha256:` over the ordered surface ids of the list (the pause rule's "steering list fixed")."""
    return sha256_id([t.surface_id for t in targets])


def odd_input_values(corpus: OddInputCorpus, seed: int, size: int | None = None) -> tuple[str, ...]:
    """The `odd_inputs.<id>` references the Explorer may type this run (choice 3)."""
    return tuple(e.ref for e in sample(corpus, seed, size).entries)
