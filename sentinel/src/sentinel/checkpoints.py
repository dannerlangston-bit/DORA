"""The one enumeration of checkpoint ids (4.1 grammar; 2.4, 2.5.1, 2.5.2, 2.11, 4.5 `census`, 4.7 checkpoint ids).

The lint (which checkpoint ids a regions file, mask, mutant or rename may name: L-RGN-01, L-XREF-04) and the
scope computation (which checkpoints a run plans, 2.11) both build checkpoint ids here, so they never disagree
on which ids exist. Every rule that decides whether an id exists is applied in this module only:

- census visits (2.11): a PR run visits a surface in the workspaces of its `census.workspaces`; the merge pass
  visits the tier A workspaces and the RC pass the tier A and B workspaces that host the surface
  (`gadgets_present`), list it in `census.workspaces`, or any of them for a global surface (kinds tab, subtab, menu,
  settings_page and dialog, scope choice 3): `pass_visits`.
- census ids (2.4, 4.5 `census.dimensions`): `census/<surface>/<workspace>`. A tier-1 surface that declares
  `census.dimensions` repeats over the cross product of the declared values, in tier A workspaces only, with the
  dimension segment `/<dim>=<value>[,<dim>=<value>]` (dimensions and values in declared order); in every other
  workspace, and for every other surface, the plain id.
- sweep (2.5.1, 2.5.2): `sweep/<surface>/<host>/<w>x<h>`; only the flexgrid, detached and dialog hosts take
  w x h sizes (tab and sidebar hosts are swept through their splitters only); a step of Ladder F's ascending pass
  takes the suffix `-asc`.
- split (2.5.2): `split/<surface>/<splitter>/<fraction>` with two decimals; the short or full fraction set,
  narrowed by the splitter's `fractions`, with 0.00 and 1.00 only when `collapsible`.
- flow (4.7): `flow/<surface_id>/<flow_id>/<step id>`.

Choices where the spec is ambiguous (reported):

- The cross product "is visited in tier-1 workspaces only" (2.2) is read as the tier A canonical workspaces of 2.3,
  as the scope computation already had it (scope choice 4).
- `surface_checkpoints`, the lint's definition (L-RGN-01: "a flow or census entry references the checkpoint"),
  takes the census workspaces of the surface's own `census.workspaces`. The merge and RC passes also visit hosting
  tier A and B workspaces the entry does not list (`pass_visits`); defining those ids too would require a regions
  file with a rule or mask region for every such masked_live visit (L-SRF-08, L-XREF-03), which the example lacks
  for the Position Statement in two tier A workspaces.
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
PASS_TIERS: dict[str, tuple[str, ...]] = {"merge": ("A",), "rc": ("A", "B")}     # 2.11 merge and RC census passes
GLOBAL_KINDS = frozenset({"tab", "subtab", "menu", "settings_page", "dialog"})    # 2.4 global surfaces (scope choice 3)


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


def pass_visits(surface_id: str, kind: object, listed: Iterable[str], workspace_id: str,
                gadgets_present: Iterable[str]) -> bool:
    """A merge or RC census pass visits the surface in this workspace (2.11): the workspace hosts it, the surface
    lists the workspace in `census.workspaces`, or the surface is global."""
    return surface_id in set(gadgets_present) or workspace_id in set(listed) or kind in GLOBAL_KINDS


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


def surface_checkpoints(surface: Mapping[str, object], workspaces: Mapping[str, Mapping[str, object]],
                        steps: Iterable[SweepStep]) -> list[tuple[str, str]]:
    """Every census, sweep and split checkpoint id one registry entry (4.5) can produce in any run type, as
    (checkpoint id, kind), in definition order and without duplicates.

    `workspaces` maps a corpus workspace id to its entry (`tier`); a census workspace absent from it is visited with
    the plain id. `steps` is the union of the ladders' steps and the surface's corpus sizes."""
    sid = str(surface.get("id"))
    out: dict[str, str] = {}
    census = _map(surface.get("census"))
    if census.get("enabled", True):
        dims = {str(k): _list(v) for k, v in _map(census.get("dimensions")).items()}
        for ws in (w for w in _list(census.get("workspaces")) if isinstance(w, str)):
            for cid in census_ids(sid, ws, surface_tier=surface.get("tier"),
                                  workspace_tier=_map(workspaces.get(ws)).get("tier"), dimensions=dims):
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
