"""The one enumeration of checkpoint ids (4.1 grammar; 2.4, 2.5.1, 2.5.2, 4.5 `census`, 4.7 checkpoint ids).

The lint (which checkpoint ids a regions file, mask, mutant or rename may name: L-RGN-01, L-XREF-04) and the
scope computation (which checkpoints a run plans, 2.11) both build checkpoint ids here, so they never disagree
on which ids exist. Every rule that decides whether an id exists is applied in this module only:

- census (2.4, 4.5 `census.dimensions`): `census/<surface>/<workspace>`. A tier-1 surface that declares
  `census.dimensions` repeats over the cross product of the declared values, in tier A workspaces only, with the
  dimension segment `/<dim>=<value>[,<dim>=<value>]` (dimensions and values in declared order); in every other
  workspace, and for every other surface, the plain id.
- sweep (2.5.1, 2.5.2): `sweep/<surface>/<host>/<w>x<h>`; only the flexgrid, detached and dialog hosts take
  w x h sizes (tab and sidebar hosts are swept through their splitters only); a step of Ladder F's ascending pass
  takes the suffix `-asc`.
- split (2.5.2): `split/<surface>/<splitter>/<fraction>` with two decimals; the short or full fraction set,
  narrowed by the splitter's `fractions`, with 0.00 and 1.00 only when `collapsible`.
- flow (4.7): `flow/<surface_id>/<flow_id>/<step id>`.

Choice where the spec is ambiguous (reported): the cross product "is visited in tier-1 workspaces only" (2.2) is
read as the tier A canonical workspaces of 2.3, as the scope computation already had it (scope choice 4).
"""
from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .taxonomy import SWEEP_HOSTS

CENSUS, SWEEP, SPLIT, FLOW = "census", "sweep", "split", "flow"
FULL_FRACTIONS: tuple[float, ...] = (0.00, 0.15, 0.30, 0.50, 0.70, 0.85, 1.00)      # 2.5.2
SHORT_FRACTIONS: tuple[float, ...] = (0.15, 0.50, 0.85)
DIMENSION_TIER = 1                       # 2.4: the census repeats tier-1 surfaces over their dimensions
DIMENSION_WORKSPACE_TIER = "A"           # 2.2: in the canonical (tier A) workspaces only


def repeats_dimensions(surface_tier: object, workspace_tier: object, dimensions: Mapping[str, Sequence] | None) -> bool:
    """True when a census visit of this surface in this workspace repeats per dimension combination (2.4)."""
    return bool(dimensions) and surface_tier == DIMENSION_TIER and str(workspace_tier) == DIMENSION_WORKSPACE_TIER


def census_ids(surface_id: str, workspace_id: str, *, surface_tier: object, workspace_tier: object,
               dimensions: Mapping[str, Sequence] | None = None) -> tuple[str, ...]:
    """The census checkpoint ids of one surface in one workspace (2.4, 4.1 census grammar)."""
    base = f"census/{surface_id}/{workspace_id}"
    if not repeats_dimensions(surface_tier, workspace_tier, dimensions):
        return (base,)
    dims = [(str(k), [str(v) for v in vals]) for k, vals in (dimensions or {}).items()]
    combos = itertools.product(*[[f"{k}={v}" for v in vals] for k, vals in dims])
    return tuple(f"{base}/{','.join(combo)}" for combo in combos)


def sweep_hosts(hosts: Iterable[object]) -> tuple[str, ...]:
    """The hosts of a surface that take w x h sweep steps (2.5.1): flexgrid, detached and dialog."""
    return tuple(h for h in hosts if isinstance(h, str) and h in SWEEP_HOSTS)


def sweep_id(surface_id: str, host: str, w: int, h: int, *, ascending: bool = False) -> str:
    """`sweep/<surface>/<host>/<w>x<h>`, with `-asc` for a step of Ladder F's ascending pass (2.5.2)."""
    return f"sweep/{surface_id}/{host}/{w}x{h}" + ("-asc" if ascending else "")


def split_fractions(fractions: Iterable[object] | None, collapsible: bool, *, full: bool) -> tuple[float, ...]:
    """The short or full fraction set of 2.5.2 narrowed by a splitter's `fractions`; 0.00 and 1.00 only when the
    splitter is collapsible."""
    keep = [f for f in (FULL_FRACTIONS if full else SHORT_FRACTIONS) if collapsible or f not in (0.0, 1.0)]
    if fractions is not None:
        allowed = {round(float(f), 2) for f in fractions
                   if isinstance(f, (int, float)) and not isinstance(f, bool)}
        keep = [f for f in keep if round(f, 2) in allowed]
    return tuple(keep)


def split_id(surface_id: str, splitter_id: str, fraction: float) -> str:
    """`split/<surface>/<splitter>/<fraction>` with the fraction as two decimals (4.1)."""
    return f"split/{surface_id}/{splitter_id}/{float(fraction):.2f}"


def flow_checkpoint_id(surface_id: str, flow_id: str, step_id: str) -> str:
    """`flow/<surface_id>/<flow_id>/<step id>` (4.7 `checkpoint_id`, L-FLW-03)."""
    return f"flow/{surface_id}/{flow_id}/{step_id}"


@dataclass(frozen=True)
class SweepStep:
    """One w x h step of a ladder (2.5.2); `host` set only for a corpus size measured on one host."""
    w: int
    h: int
    ascending: bool = False
    host: str | None = None


def _map(x: object) -> Mapping[str, object]:
    return x if isinstance(x, Mapping) else {}


def _list(x: object) -> list:
    return x if isinstance(x, list) else []


def surface_checkpoints(surface: Mapping[str, object], workspace_tiers: Mapping[str, object],
                        steps: Iterable[SweepStep]) -> list[tuple[str, str]]:
    """Every census, sweep and split checkpoint id one registry entry (4.5) can produce in any run type, as
    (checkpoint id, kind), in definition order and without duplicates.

    `workspace_tiers` maps a corpus workspace id to its tier (a census workspace absent from it is visited with
    the plain id); `steps` is the union of the ladders' steps and the surface's corpus sizes (2.5.2)."""
    sid = str(surface.get("id"))
    out: dict[str, str] = {}
    census = _map(surface.get("census"))
    if census.get("enabled", True):
        dims = {str(k): _list(v) for k, v in _map(census.get("dimensions")).items()}
        for ws in _list(census.get("workspaces")):
            if isinstance(ws, str):
                for cid in census_ids(sid, ws, surface_tier=surface.get("tier"), workspace_tier=workspace_tiers.get(ws),
                                      dimensions=dims):
                    out.setdefault(cid, CENSUS)
    if surface.get("resizable") is True:
        hosts = sweep_hosts(_list(surface.get("hosts")))
        for st in steps:
            for host in hosts:
                if st.host in (None, host):
                    out.setdefault(sweep_id(sid, host, st.w, st.h, ascending=st.ascending), SWEEP)
    for sp in map(_map, _list(surface.get("splitters"))):
        if isinstance(sp.get("id"), str):
            fr = sp.get("fractions")
            for f in split_fractions(fr if isinstance(fr, list) else None, sp.get("collapsible") is True, full=True):
                out.setdefault(split_id(sid, str(sp["id"]), f), SPLIT)
    return list(out.items())
