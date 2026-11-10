"""Scope computation per run type: the Dependency Map, the static impact step and the plan.

Spec: 2.10 (Dependency Map, static impact step), 2.11 (scope per run type, common filters,
order-and-cut, smoke subset, the no-manifest cases), 2.5.2 and 2.5.3 (ladders and which surfaces
get which ladder), 3.1 (frozen base reuse, masked_live lockstep), 4.3 (change manifest), 4.4
(dependency map), 1.8 and 9.4 (`missed_in_pr_run`).

`compute_scope` returns a `ScopePlan`: the planned checkpoints in run order and per rig, the cut
list (`not_run` with reason `entitlement`, `quarantine`, `budget` or `scope`), the run-level
findings (`dependency_map_gap`, `manifest_missing`) and `manifest_state`, the flag the classifier
reads as input X of 3.11 (`ScopePlan.x_input`).

Choices where the spec is ambiguous (each is reported to the coordinator):

1. Sweeps and flows follow the change-derived scope, not the smoke census (2.11). The pseudocode
   sweeps and selects flows over `census_surfaces`, which includes the smoke subset; 1.6, 1.11 and
   the PR budget of 11.2 (75 sweep steps, 8 tier-1 flows) only hold if the smoke subset adds
   census checkpoints alone. Here sweeps and flows use declared, map-expanded and static surfaces.
2. PR census workspaces (2.11, 1.11). The pseudocode visits the canonical workspace only; 1.11
   visits the declared chart cell in both its tier A workspaces. Declared surfaces are visited in
   every workspace of `census.workspaces`; every other surface in the canonical one only.
3. Global surfaces (2.4 "tabs, sub-tabs, Setup entries, settings pages, and dialogs"): kinds `tab`,
   `subtab`, `menu`, `settings_page` and `dialog`. Hosting is `gadgets_present` of the workspace or
   the workspace being listed in the surface's `census.workspaces`.
4. Census dimensions (2.2.2, 2.4, 4.5): tier-1 surfaces repeat the cross product of
   `census.dimensions` in tier A workspaces, one checkpoint per combination; elsewhere the plain id.
5. Sweep and split checkpoints run in the tier C harness workspace; their persona is that
   workspace's persona tag or else the surface's first persona (the outcome record needs one).
6. Sharding (2.11): stable hash (sha256) of the item id over the rigs; flows hash on their fixture
   so flows sharing a fixture stay on one rig; sweep steps hash on (surface, host) and split steps
   on (surface, splitter) so a ladder runs in its mandatory order in one session (2.5.1, 2.5.2).
   `masked_live` items take a pair of identically fingerprinted rigs; with no such pair they are
   `not_run (reason: budget)` (3.1). A flow with any `masked_live` checkpoint is a lockstep item.
   The soak rig and the Explorer rig are outside the pool (1.9, 11.3).
7. Cost on a rig: per-side duration from the run database (defaults 11.2: census visit 12 s, sweep
   or split step 8 s, flow 90 s, soak cycle 15 min) times 2 for a frozen item without a reusable
   base (base then candidate on one rig) and times 1 with one, reuse being decided against the
   fingerprint of the rig the item is placed on; a `masked_live` item costs its
   per-side duration on each rig of its pair. Session startups are not modelled. The budget is the
   pseudocode's B (PR 45 min, merge 3 h, RC 8 h - 10 min - 90 min) unless overridden.
8. Ladder F's ascending pass revisits six grid sizes whose checkpoint ids the grammar of 4.1 cannot
   tell apart from the descending pass; the plan lists each id once. Ladder overrides
   (`size_ladder`), the streaming-surface exclusion and the chart rule under DR-02 branch B of 2.5.3
   carry no registry field here and are not applied.
9. Static impact (2.10.2): depth 0 is the changed class itself. A surface reached only through its
   own roots is not a map gap (no primitive is involved); a changed class owned by another surface
   but no primitive is named as the unowned root of the missing edge. Without a dependency index
   the unowned-root check is skipped with the rest of the static step, so exactly one gap appears.
10. `missed_in_pr_run` (1.8, 9.4): "had in scope" means the PR's change-derived scope (a smoke-only
    surface does not attribute a merge finding to every PR); "not_run (reason: scope)" means outside
    its census scope. Missable finding kinds are those of the Unexpected list: `collateral`,
    `invariant_violation`, `assertion_failed`, `needs_human`, `landmark_missing`.
11. Finding ids are `fnd-<run_id>-scope-<nnn>` and `fnd-<run_id>-merge-<nnn>`; gap details go in
    `evidence.raw_values` (4.13 carries no reason on a finding).
"""
from __future__ import annotations

import fnmatch
import hashlib
import itertools
from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator, Mapping, Sequence

from .records import DATA_MODES, PERSONAS, CheckpointOutcome, Finding
from .taxonomy import is_checkpoint_id

SMOKE_CAP = 40                                   # 2.2.4, L-SRF-15
STATIC_MAX_DEPTH = 6                             # 2.10.2
EXPLORER_BUDGET_S = 90 * 60                      # 2.9
BUDGET_S = {"pr": 45 * 60, "merge": 3 * 3600, "rc": 8 * 3600 - 10 * 60 - EXPLORER_BUDGET_S}   # 2.11
DEFAULT_DURATION_S = {"census": 12.0, "sweep": 8.0, "split": 8.0, "flow": 90.0}               # 11.2
SOAK_CYCLES = 24                                 # 1.9, 10.11
SOAK_CYCLE_S = 6 * 3600 / SOAK_CYCLES
LADDER_S = tuple((w, h) for w in (960, 640, 480, 320) for h in (720, 480, 360))               # 2.5.2
LADDER_F_GRID = tuple((w, h) for w in (960, 800, 640, 480, 400, 320) for h in (720, 600, 480, 420, 360, 300))
FULL_FRACTIONS = (0.00, 0.15, 0.30, 0.50, 0.70, 0.85, 1.00)
SHORT_FRACTIONS = (0.15, 0.50, 0.85)
GLOBAL_KINDS = frozenset({"tab", "subtab", "menu", "settings_page", "dialog"})
KIND_ORDER = {"soak": 0, "flow": 0, "sweep": 1, "split": 2, "census": 3}
MISSABLE_KINDS = frozenset({"collateral", "invariant_violation", "assertion_failed", "needs_human", "landmark_missing"})


class ScopeConfigError(ValueError):
    """An input lint should have refused (2.2.4, 4.15); the run ends aborted_harness (config_invalid)."""


# -- matching ---------------------------------------------------------------------------------

def class_matches(cls: str, root: str) -> bool:
    """True when a class lies under a root: a package or class prefix, or a Java package glob.

    Prefixes match on segment boundaries (`a.b` covers `a.b.C` and `a.b$Inner`); in a glob `**`
    matches any number of segments and `*` one segment (2.10.1 `source_packages`, 4.5 `roots`).
    """
    if "*" not in root and "?" not in root:
        return cls == root or cls.startswith(root + ".") or cls.startswith(root + "$")
    return _segments_match(cls.split("."), root.split("."))


def _segments_match(parts: list[str], pats: list[str]) -> bool:
    if not pats:
        return not parts
    if pats[0] == "**":
        return any(_segments_match(parts[i:], pats[1:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pats[0]) and _segments_match(parts[1:], pats[1:])


def classes_in(paths: Iterable[str]) -> set[str]:
    """`classes_in(delta)` of 2.11: Java source paths of the diff to class names.

    The class name is the path after the last `/java/` source root, without `.java`; non-Java
    paths carry no class.
    """
    out = set()
    for p in paths:
        if not p.endswith(".java"):
            continue
        tail = p.rsplit("/java/", 1)[-1] if "/java/" in p else p
        out.add(tail[: -len(".java")].strip("/").replace("/", "."))
    return out


def _stable_hash(key: str) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], 16)


# -- inputs: registry, corpus, flows (4.5, 4.6, 4.7) ------------------------------------------

@dataclass(frozen=True)
class Splitter:
    """An in-host splitter of a registry entry (2.2.1, 4.5)."""
    id: str
    host: str
    fractions: tuple[float, ...] | None = None
    collapsible: bool = False

    def steps(self, full: bool) -> tuple[float, ...]:
        """The short or full fraction set of 2.5.2 narrowed by `fractions`; 0.00 and 1.00 only when collapsible."""
        base = FULL_FRACTIONS if full else SHORT_FRACTIONS
        keep = [f for f in base if self.collapsible or f not in (0.0, 1.0)]
        if self.fractions is not None:
            allowed = {round(f, 2) for f in self.fractions}
            keep = [f for f in keep if round(f, 2) in allowed]
        return tuple(keep)


@dataclass(frozen=True)
class Surface:
    """The fields of a Surface Registry entry (4.5) that scope and coverage read."""
    id: str
    tier: int
    kind: str = "gadget"
    smoke: bool = False
    primitives: tuple[str, ...] = ()
    roots: tuple[str, ...] = ()
    personas: tuple[str, ...] = ()
    entitlements: frozenset[str] = frozenset()
    resizable: bool = False
    hosts: tuple[str, ...] = ()
    splitters: tuple[Splitter, ...] = ()
    data_mode: str = "frozen"
    census_enabled: bool = True
    census_workspaces: tuple[str, ...] = ()
    census_dimensions: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @property
    def is_global(self) -> bool:
        """Visited in every workspace of the pass (2.4); see choice 3 in the module docstring."""
        return self.kind in GLOBAL_KINDS

    @classmethod
    def from_dict(cls, d: Mapping) -> "Surface":
        census = d.get("census") or {}
        return cls(
            id=d["id"], tier=int(d["tier"]), kind=d.get("kind", "gadget"), smoke=bool(d.get("smoke", False)),
            primitives=tuple(d.get("primitives") or ()), roots=tuple(d.get("roots") or ()),
            personas=tuple(d.get("personas") or ()), entitlements=frozenset(d.get("entitlements") or ()),
            resizable=bool(d.get("resizable", False)), hosts=tuple(d.get("hosts") or ()),
            splitters=tuple(Splitter(s["id"], s.get("host", "tab"),
                                     tuple(s["fractions"]) if s.get("fractions") is not None else None,
                                     bool(s.get("collapsible", False))) for s in d.get("splitters") or ()),
            data_mode=d.get("data_mode", "frozen"), census_enabled=bool(census.get("enabled", True)),
            census_workspaces=tuple(census.get("workspaces") or ()),
            census_dimensions=tuple((k, tuple(str(v) for v in vs)) for k, vs in (census.get("dimensions") or {}).items()),
        )


@dataclass
class Registry:
    """The Surface Registry R at the build's registry commit (2.2)."""
    surfaces: dict[str, Surface]

    @classmethod
    def from_entries(cls, entries: Iterable[Mapping | Surface]) -> "Registry":
        out: dict[str, Surface] = {}
        for e in entries:
            s = e if isinstance(e, Surface) else Surface.from_dict(e)
            if s.id in out:
                raise ScopeConfigError(f"duplicate surface id {s.id} (L-SRF-01)")
            out[s.id] = s
        return cls(out)

    def __iter__(self) -> Iterator[Surface]:
        return iter(self.surfaces[k] for k in sorted(self.surfaces))

    def __contains__(self, sid: object) -> bool:
        return sid in self.surfaces

    def __getitem__(self, sid: str) -> Surface:
        return self.surfaces[sid]

    def match(self, pattern: str) -> set[str]:
        """Registry ids an edge's surface names: an id, a glob such as `charts.*`, or `*` (4.4)."""
        if pattern in self.surfaces:
            return {pattern}
        return {sid for sid in self.surfaces if fnmatch.fnmatchcase(sid, pattern)}

    def owning_surfaces(self, cls: str) -> set[str]:
        """Surfaces whose `roots` cover a class."""
        return {s.id for s in self.surfaces.values() if any(class_matches(cls, r) for r in s.roots)}

    def smoke_subset(self) -> set[str]:
        """`smoke_subset(R)` of 2.11; more than 40 flags is a lint failure the run never absorbs."""
        smoke = {s.id for s in self.surfaces.values() if s.smoke}
        if len(smoke) > SMOKE_CAP:
            raise ScopeConfigError(f"{len(smoke)} surfaces carry smoke: true; the cap is {SMOKE_CAP} (2.2.4, L-SRF-15)")
        return smoke


@dataclass(frozen=True)
class Workspace:
    """A Workspace Corpus entry (4.6): tier, persona tag and the derived hosted-surface list."""
    id: str
    tier: str
    persona: str | None = None
    gadgets_present: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, d: Mapping) -> "Workspace":
        return cls(d["id"], str(d["tier"]), d.get("persona"), frozenset(d.get("gadgets_present") or ()))


@dataclass(frozen=True)
class FlowCheckpoint:
    """A `checkpoint` step of a flow (4.7)."""
    checkpoint_id: str
    surface_id: str
    step_id: str
    data_mode: str


@dataclass(frozen=True)
class Flow:
    """The header fields and checkpoint steps of a flow file (4.7)."""
    flow_id: str
    tier: int
    persona: str
    workspace: str
    surfaces: tuple[str, ...]
    checkpoints: tuple[FlowCheckpoint, ...]
    requires: frozenset[str] = frozenset()
    fixture: str | None = None
    data_mode: str = "frozen"
    flow_class: str = "standard"

    @classmethod
    def from_dict(cls, d: Mapping) -> "Flow":
        default_mode = d.get("data_mode", "frozen")
        cps = tuple(FlowCheckpoint(s["checkpoint_id"], s["surface_id"], s["id"], s.get("data_mode", default_mode))
                    for s in d.get("steps") or () if s.get("type") == "checkpoint")
        return cls(flow_id=d["flow_id"], tier=int(d["tier"]), persona=d["persona"], workspace=d["workspace"],
                   surfaces=tuple(d.get("surfaces") or ()), checkpoints=cps,
                   requires=frozenset(d.get("requires") or ()), fixture=d.get("fixture"),
                   data_mode=default_mode, flow_class=d.get("class", "standard"))


# -- change manifest (4.3) --------------------------------------------------------------------

@dataclass(frozen=True)
class ManifestEntry:
    """One entry of the change manifest (4.3)."""
    entry_id: str
    tickets: tuple[str, ...]
    summary: str = ""
    declared_surfaces: tuple[str, ...] = ()
    new_surface: bool = False
    touched_primitives: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    expected_visual: str = ""
    mock_image: str | None = None
    must_be_observed: bool = True

    @classmethod
    def from_dict(cls, d: Mapping) -> "ManifestEntry":
        return cls(entry_id=d["entry_id"], tickets=tuple(d.get("tickets") or ()), summary=d.get("summary", ""),
                   declared_surfaces=tuple(d.get("declared_surfaces") or ()), new_surface=bool(d.get("new_surface", False)),
                   touched_primitives=tuple(d.get("touched_primitives") or ()),
                   changed_paths=tuple(d.get("changed_paths") or ()), expected_visual=(d.get("expected_visual") or "").strip(),
                   mock_image=d.get("mock_image"), must_be_observed=bool(d.get("must_be_observed", True)))


@dataclass(frozen=True)
class Manifest:
    """The change manifest M, the only source of intent (4.3). It reaches scope already linted."""
    manifest_id: str
    candidate: str
    base: str
    source: str
    author: str
    entries: tuple[ManifestEntry, ...] = ()
    approved_by: str | None = None
    empty_reason: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping) -> "Manifest":
        d = d.get("manifest", d) if isinstance(d.get("manifest"), Mapping) else d
        return cls(manifest_id=d["manifest_id"], candidate=d["candidate"], base=d["base"], source=d["source"],
                   author=d["author"], entries=tuple(ManifestEntry.from_dict(e) for e in d.get("entries") or ()),
                   approved_by=d.get("approved_by"), empty_reason=d.get("empty_reason"))

    @classmethod
    def union(cls, manifests: Iterable["Manifest"], candidate: str, base: str, author: str,
              approved_by: str | None = None) -> "Manifest":
        """The merge and RC manifest: the union of the PR manifests in range, `source: union` (4.3)."""
        entries = tuple(e for m in manifests for e in m.entries)
        return cls(manifest_id=f"mf-{candidate}", candidate=candidate, base=base, source="union", author=author,
                   entries=entries, approved_by=approved_by,
                   empty_reason=None if entries else "no PR manifests in range")

    @property
    def surfaces(self) -> frozenset[str]:
        """`M.surfaces` of 2.11: every declared surface."""
        return frozenset(s for e in self.entries for s in e.declared_surfaces)

    @property
    def primitives(self) -> frozenset[str]:
        """`M.primitives` of 2.11: every touched primitive."""
        return frozenset(p for e in self.entries for p in e.touched_primitives)

    def entry_declaring(self, surface_id: str) -> ManifestEntry | None:
        return next((e for e in self.entries if surface_id in e.declared_surfaces), None)

    def entry_touching(self, primitive: str) -> ManifestEntry | None:
        return next((e for e in self.entries if primitive in e.touched_primitives), None)


# -- dependency map (4.4) and dependency index (2.10.2) ---------------------------------------

@dataclass(frozen=True)
class Primitive:
    """A hosting primitive of the Dependency Map (4.4)."""
    id: str
    source_packages: tuple[str, ...]
    owner: str = ""
    is_global: bool = False
    description: str = ""


@dataclass(frozen=True)
class Edge:
    """A Dependency Map edge (4.4)."""
    primitive: str
    surface: str
    source: str
    confidence: float
    confirmed_from_source: bool = False
    confirmed_build: str | None = None
    evidence: str | None = None
    reason: str | None = None
    first_seen: str | None = None
    last_verified: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class DependencyMap:
    """The committed Dependency Map D (2.10.1, 4.4)."""
    primitives: dict[str, Primitive]
    edges: list[Edge]

    @classmethod
    def from_dict(cls, d: Mapping) -> "DependencyMap":
        d = d.get("dependency_map", d) if isinstance(d.get("dependency_map"), Mapping) else d
        prims = {p["id"]: Primitive(p["id"], tuple(p.get("source_packages") or ()), p.get("owner", ""),
                                    bool(p.get("global", False)), p.get("description", ""))
                 for p in d.get("primitives") or ()}
        edges = [Edge(e["primitive"], e["surface"], e["source"], float(e["confidence"]),
                      bool(e.get("confirmed_from_source", False)), e.get("confirmed_build"), e.get("evidence"),
                      e.get("reason"), _ts(e.get("first_seen")), _ts(e.get("last_verified")))
                 for e in d.get("edges") or ()]
        return cls(prims, edges)

    def owning_primitives(self, cls: str) -> set[str]:
        """Primitives whose `source_packages` cover a class (`roots(p)` of 2.11)."""
        return {p.id for p in self.primitives.values() if any(class_matches(cls, g) for g in p.source_packages)}

    def surfaces_of(self, primitive: str, registry: Registry) -> set[str]:
        """`D[p].surfaces`: every registry surface an edge of the primitive names, globs expanded."""
        out: set[str] = set()
        for e in self.edges:
            if e.primitive == primitive:
                out |= registry.match(e.surface)
        return out

    def has_edge(self, primitive: str, surface_id: str, registry: Registry) -> bool:
        return surface_id in self.surfaces_of(primitive, registry)


def _ts(v: object) -> str | None:
    return None if v is None else str(v)


@dataclass
class DependencyIndex:
    """The candidate build's class dependency index S: class -> its direct dependents (2.10.2)."""
    dependents: dict[str, frozenset[str]]

    @classmethod
    def from_depends_on(cls, depends_on: Mapping[str, Iterable[str]]) -> "DependencyIndex":
        """Build the reverse index from a forward one (class -> classes it depends on, as jdeps prints)."""
        rev: dict[str, set[str]] = {}
        for c, deps in depends_on.items():
            for d in deps:
                rev.setdefault(d, set()).add(c)
        return cls({k: frozenset(v) for k, v in rev.items()})

    def reach(self, changed: Iterable[str], max_depth: int = STATIC_MAX_DEPTH) -> dict[str, dict[str, tuple[str, ...]]]:
        """`reverse_closure(S, changed, max_depth)` per changed class: origin -> {dependent: path}.

        Depth 0 is the changed class itself; each path runs from the changed class to the dependent.
        """
        out: dict[str, dict[str, tuple[str, ...]]] = {}
        for c in sorted(set(changed)):
            paths = {c: (c,)}
            frontier = [c]
            for _ in range(max_depth):
                nxt = []
                for x in frontier:
                    for d in sorted(self.dependents.get(x, ())):
                        if d not in paths:
                            paths[d] = paths[x] + (d,)
                            nxt.append(d)
                frontier = nxt
            out[c] = paths
        return out


# -- static impact (2.10.2) -------------------------------------------------------------------

@dataclass(frozen=True)
class MapGap:
    """A missing Dependency Map edge, or an unowned changed class, found by the static impact step."""
    reason: str                       # missing_edge | unowned_root
    surface: str | None
    primitive: str | None = None
    unowned_root: str | None = None
    evidence: str = ""


@dataclass(frozen=True)
class StaticImpact:
    """The static impact step's result for a PR run (2.10.2, 2.11 `scope_pr`)."""
    changed: frozenset[str]
    touched_primitives: frozenset[str]
    map_expanded: frozenset[str]
    static_surfaces: frozenset[str]
    gaps: tuple[MapGap, ...]
    index_present: bool
    paths: Mapping[str, tuple[str, ...]] = field(default_factory=dict)   # surface -> class chain that reached it


def static_impact(changed_classes: Iterable[str], dep_map: DependencyMap, registry: Registry,
                  dep_index: DependencyIndex | None, manifest: Manifest | None,
                  max_depth: int = STATIC_MAX_DEPTH) -> StaticImpact:
    """Changed classes -> touched primitives, map expansion, static surfaces and map gaps (2.10.2, 2.11)."""
    changed = frozenset(changed_classes)
    declared = manifest.surfaces if manifest else frozenset()
    owners = {c: dep_map.owning_primitives(c) for c in changed}
    touched = frozenset((manifest.primitives if manifest else frozenset()) | {p for ps in owners.values() for p in ps})
    map_expanded = frozenset(s for p in touched for s in dep_map.surfaces_of(p, registry))
    if dep_index is None:
        return StaticImpact(changed, touched, map_expanded, frozenset(), (), False)

    reach = dep_index.reach(changed, max_depth)
    best: dict[tuple[str, str], tuple[str, ...]] = {}       # (surface, changed class) -> shortest path into its roots
    for s in registry:
        for origin, reached in reach.items():
            hits = [path for cls, path in reached.items() if any(class_matches(cls, r) for r in s.roots)]
            if hits:
                best[(s.id, origin)] = min(hits, key=lambda p: (len(p), p))
    static_surfaces = frozenset(sid for sid, _ in best)
    paths: dict[str, tuple[str, ...]] = {}
    for (sid, _), path in sorted(best.items(), key=lambda kv: (kv[0][0], len(kv[1]), kv[1])):
        paths.setdefault(sid, path)

    gaps: dict[tuple, MapGap] = {}
    for (sid, origin), path in sorted(best.items()):
        if sid in map_expanded or sid in declared:
            continue
        chain = " -> ".join(path)
        if owners[origin]:
            for p in sorted(owners[origin]):
                gaps.setdefault(("missing_edge", sid, p), MapGap("missing_edge", sid, primitive=p, evidence=chain))
        elif not any(class_matches(origin, r) for r in registry[sid].roots):   # a surface's own code is no map gap
            gaps.setdefault(("missing_edge", sid, origin),
                            MapGap("missing_edge", sid, unowned_root=origin, evidence=chain))
    for c in sorted(changed):
        if not owners[c] and not registry.owning_surfaces(c):
            gaps.setdefault(("unowned_root", c), MapGap("unowned_root", None, unowned_root=c, evidence=c))
    return StaticImpact(changed, touched, map_expanded, static_surfaces, tuple(gaps.values()), True, paths)


# -- the plan ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rig:
    """A rig of the run's pool with the fingerprint it was provisioned under (3.1, 11.3)."""
    id: str
    fingerprint_id: str = "default"


@dataclass(frozen=True)
class StoredBase:
    """A stored frozen base capture and the identity it was captured under (3.1)."""
    source_run_id: str
    fingerprint_id: str
    data_epoch_id: str
    region_version: str = ""
    redaction_version: str = ""


@dataclass(frozen=True)
class BaseReuse:
    """The live identity a stored frozen base capture must equal to be reused (3.1)."""
    fingerprint_id: str
    data_epoch_id: str
    stored: Mapping[str, StoredBase]
    redaction_version: str = ""
    region_versions: Mapping[str, str] = field(default_factory=dict)

    def source_for(self, checkpoint_id: str, fingerprint_id: str | None = None) -> str | None:
        """The run whose base capture this checkpoint reuses, or None when it must be re-captured.
        `fingerprint_id` is the live fingerprint of the rig the item runs on, when known."""
        b = self.stored.get(checkpoint_id)
        if b is None:
            return None
        live_fp = self.fingerprint_id if fingerprint_id is None else fingerprint_id
        same = (b.fingerprint_id == live_fp and b.data_epoch_id == self.data_epoch_id
                and b.redaction_version == self.redaction_version
                and b.region_version == self.region_versions.get(checkpoint_id, ""))
        return b.source_run_id if same else None


@dataclass(frozen=True)
class SoakPlan:
    """The RC soak loop on rig 5: 24 cycle checkpoints `flow/<surface>/soak/c<nn>` (1.9, 10.11)."""
    rig: Rig
    workspace_id: str
    surface_id: str


@dataclass(frozen=True)
class PlannedCheckpoint:
    """One checkpoint in the plan with the metadata its outcome record needs (4.13)."""
    checkpoint_id: str
    surface_id: str
    test_kind: str
    tier: int
    workspace_id: str
    persona: str
    data_mode: str
    flow_id: str | None = None
    step_id: str | None = None
    size_step: tuple[tuple[str, object], ...] | None = None
    base_source_run_id: str | None = None

    def outcome(self, run_id: str, outcome: str, reason: str | None = None, gate: str | None = None) -> CheckpointOutcome:
        """An outcome record for this checkpoint (validated against the taxonomy on construction)."""
        return CheckpointOutcome(run_id=run_id, checkpoint_id=self.checkpoint_id, outcome=outcome, reason=reason,
                                 gate=gate, workspace_id=self.workspace_id, persona=self.persona,
                                 data_mode=self.data_mode, surface_id=self.surface_id, test_kind=self.test_kind,
                                 tier=self.tier, flow_id=self.flow_id, step_id=self.step_id,
                                 size_step=dict(self.size_step) if self.size_step else None)


@dataclass
class PlanItem:
    """A schedulable unit of 2.11: a census visit, a sweep or split step, a flow or a soak cycle."""
    item_id: str
    kind: str
    tier: int
    checkpoints: list[PlannedCheckpoint]
    data_mode: str
    shard_key: str
    protected: bool
    estimated_s: float
    order: int
    rigs: tuple[str, ...] = ()

    @property
    def rig_cost_s(self) -> float:
        """Time on each of its rigs (choice 7): a frozen item without a reusable base runs twice."""
        if self.kind == "soak" or self.data_mode == "masked_live":
            return self.estimated_s
        reused = all(cp.base_source_run_id for cp in self.checkpoints)
        return self.estimated_s * (1 if reused else 2)


@dataclass(frozen=True)
class Cut:
    """A scoped item or surface that will not run: `not_run` with this reason (2.11)."""
    reason: str                                  # entitlement | quarantine | budget | scope
    surface_id: str
    tier: int
    checkpoint: PlannedCheckpoint | None = None  # None: the whole surface is outside the PR scope

    @property
    def persona(self) -> str | None:
        return self.checkpoint.persona if self.checkpoint else None

    def outcome(self, run_id: str) -> CheckpointOutcome:
        if self.checkpoint is None:
            raise ValueError(f"surface-level cut of {self.surface_id} has no checkpoint id")
        return self.checkpoint.outcome(run_id, "not_run", self.reason)


@dataclass(frozen=True)
class Attribution:
    """The primitive and manifest entry a surface's diffs are posted with (1.11, 4.13 `collateral`)."""
    primitive: str | None
    entry_id: str | None


@dataclass(frozen=True)
class PrRunSummary:
    """What a merge run needs from each merged PR's run to record `missed_in_pr_run` (1.8)."""
    run_id: str
    change_scope: frozenset[str]
    census_surfaces: frozenset[str]
    reported_surfaces: frozenset[str]
    changed_classes: frozenset[str]
    touched_primitives: frozenset[str]


@dataclass
class ScopePlan:
    """The output of `compute_scope`: planned checkpoints, cuts, run findings and the manifest state."""
    run_id: str
    run_type: str
    budget_s: float
    manifest_state: str                  # loaded | no_manifest_ui (case a) | no_manifest_nonui (case b), 4.3
    declared: frozenset[str]
    scope_surfaces: frozenset[str]
    census_surfaces: frozenset[str]
    items: list[PlanItem]
    by_rig: dict[str, list[PlanItem]]
    cuts: list[Cut]
    findings: list[Finding]
    projected_overrun_s: dict[str, float]
    attribution: dict[str, Attribution]
    proposed_edges: list[Edge]
    impact: StaticImpact | None = None
    explorer_rig: str | None = None

    @property
    def planned(self) -> list[PlannedCheckpoint]:
        """Every planned checkpoint in run order (tier, flows, sweeps, census, longest first)."""
        return [cp for it in self.items for cp in it.checkpoints]

    @property
    def planned_ids(self) -> list[str]:
        return [cp.checkpoint_id for cp in self.planned]

    @property
    def intended_enabled(self) -> bool:
        """False without a manifest: no diff can be `intended` (4.3)."""
        return self.manifest_state == "loaded"

    def x_input(self, surface_id: str) -> str:
        """Input X of 3.11 for a checkpoint on this surface."""
        if self.manifest_state != "loaded":
            return self.manifest_state
        return "declared" if surface_id in self.declared else "undeclared"

    def unreached_outcomes(self, reached: Iterable[str], run_status: str, run_reason: str | None = None,
                           gate: str | None = None) -> list[CheckpointOutcome]:
        """Outcomes of planned checkpoints an aborted run never reached, by the abort's class (2.11, 1.10)."""
        done = set(reached)
        todo = [cp for cp in self.planned if cp.checkpoint_id not in done]
        if run_status == "aborted_budget":
            return [cp.outcome(self.run_id, "not_run", "budget") for cp in todo]
        if run_status == "aborted_safety":
            return [cp.outcome(self.run_id, "env_error", run_reason, gate) for cp in todo]
        if run_status == "aborted_harness":
            return [cp.outcome(self.run_id, "harness_error", run_reason) for cp in todo]
        raise ValueError(f"{run_status} is not an abort")

    def deadline_outcomes(self, reached: Iterable[str]) -> tuple[list[CheckpointOutcome], tuple[str, str] | None]:
        """At the budget deadline: unreached checkpoints `not_run (budget)` and the run `aborted_budget (wall_clock)`."""
        outs = self.unreached_outcomes(reached, "aborted_budget")
        return outs, (("aborted_budget", "wall_clock") if outs else None)

    def pr_summary(self, reported_surfaces: Iterable[str]) -> PrRunSummary:
        """The PR run's summary a later merge run compares its findings against (1.8)."""
        if self.run_type != "pr":
            raise ValueError("only a PR run has a PR summary")
        imp = self.impact
        return PrRunSummary(self.run_id, self.scope_surfaces, self.census_surfaces, frozenset(reported_surfaces),
                            imp.changed if imp else frozenset(), imp.touched_primitives if imp else frozenset())


# -- compute_scope ----------------------------------------------------------------------------

@dataclass
class _Ctx:
    run_type: str
    registry: Registry
    workspaces: dict[str, Workspace]
    protected_surfaces: frozenset[str]
    durations: Mapping[str, float]
    base_reuse: BaseReuse | None
    max_client: tuple[int, int]
    corpus_sizes: Mapping[str, Sequence[tuple[int, int]]]
    seq: Iterator[int] = field(default_factory=itertools.count)

    def workspace(self, wid: str) -> Workspace:
        if wid not in self.workspaces:
            raise ScopeConfigError(f"workspace {wid} is not in the corpus (L-SRF-10)")
        return self.workspaces[wid]

    def persona(self, w: Workspace, s: Surface | None) -> str:
        persona = w.persona or (s.personas[0] if s and s.personas else None)
        if persona not in PERSONAS:
            raise ScopeConfigError(f"no persona for workspace {w.id}")
        return persona

    def harness(self) -> Workspace:
        tier_c = sorted(w.id for w in self.workspaces.values() if w.tier == "C")
        if not tier_c:
            raise ScopeConfigError("size sweeps need the tier C harness workspace (2.3)")
        return self.workspaces[tier_c[0]]

    def checkpoint(self, cid: str, s: Surface, kind: str, w: Workspace, data_mode: str,
                   persona: str | None = None, base: bool = True, **kw) -> PlannedCheckpoint:
        """A planned checkpoint; `base=False` for candidate-only work (the soak, 1.9)."""
        if not is_checkpoint_id(cid):
            raise ScopeConfigError(f"checkpoint id {cid!r} is outside the grammar of 4.1")
        if data_mode not in DATA_MODES:
            raise ScopeConfigError(f"data_mode {data_mode!r} on {cid}")
        reuse = self.base_reuse.source_for(cid) if base and self.base_reuse and data_mode == "frozen" else None
        return PlannedCheckpoint(cid, s.id, kind, s.tier, w.id, persona or self.persona(w, s), data_mode,
                                 base_source_run_id=reuse, **kw)

    def item(self, item_id: str, kind: str, tier: int, cps: list[PlannedCheckpoint], data_mode: str,
             shard_key: str, protected: bool, estimated_s: float) -> PlanItem:
        return PlanItem(item_id, kind, tier, cps, data_mode, shard_key, protected, estimated_s, next(self.seq))

    def duration(self, key: str, kind: str) -> float:
        return float(self.durations.get(key, DEFAULT_DURATION_S[kind]))


def _census_items(ctx: _Ctx, s: Surface, workspace_ids: Iterable[str]) -> list[PlanItem]:
    out = []
    for wid in workspace_ids:
        w = ctx.workspace(wid)
        base = f"census/{s.id}/{w.id}"
        if s.tier == 1 and s.census_dimensions and w.tier == "A":
            names = [k for k, _ in s.census_dimensions]
            combos = itertools.product(*(vs for _, vs in s.census_dimensions))
            ids = [base + "/" + ",".join(f"{k}={v}" for k, v in zip(names, combo)) for combo in combos]
        else:
            ids = [base]
        cps = [ctx.checkpoint(cid, s, "census", w, s.data_mode) for cid in ids]
        est = sum(ctx.duration(cid, "census") for cid in ids)
        out.append(ctx.item(base, "census", s.tier, cps, s.data_mode, base, s.id in ctx.protected_surfaces, est))
    return out


def _ladder(s: Surface, ctx: _Ctx) -> tuple[tuple[tuple[int, int], ...], bool | None]:
    """2.5.3: (sizes, full splitter set?) for a resizable surface in this run type; no sweep -> ((), None)."""
    if ctx.run_type == "pr":
        return (LADDER_S, False) if s.tier == 1 else ((), None)
    if ctx.run_type == "merge":
        return (LADDER_S, False) if s.tier <= 2 else ((), None)
    if s.tier <= 2:
        maxw, maxh = ctx.max_client
        corners = ((240, 160), (240, maxh), (maxw, 160), (maxw, maxh))
        ascending = tuple((w, 480) for w in (320, 400, 480, 640, 800, 960))
        sizes = LADDER_F_GRID + corners + tuple(ctx.corpus_sizes.get(s.id, ())) + ascending
        return tuple(dict.fromkeys(sizes)), True
    return LADDER_S, None


def _sweep_items(ctx: _Ctx, s: Surface) -> list[PlanItem]:
    if not s.resizable:
        return []
    sizes, full = _ladder(s, ctx)
    if not sizes:
        return []
    w = ctx.harness()
    protected = s.id in ctx.protected_surfaces
    out = []
    for host in s.hosts:
        for (wd, ht) in sizes:
            cid = f"sweep/{s.id}/{host}/{wd}x{ht}"
            cp = ctx.checkpoint(cid, s, "sweep", w, s.data_mode, size_step=(("host", host), ("w", wd), ("h", ht)))
            out.append(ctx.item(cid, "sweep", s.tier, [cp], s.data_mode, f"sweep:{s.id}:{host}", protected,
                                ctx.duration(cid, "sweep")))
    if full is not None:
        for sp in s.splitters:
            for frac in sp.steps(full):
                cid = f"split/{s.id}/{sp.id}/{frac:.2f}"
                cp = ctx.checkpoint(cid, s, "split", w, s.data_mode, size_step=(("splitter", sp.id), ("fraction", frac)))
                out.append(ctx.item(cid, "split", s.tier, [cp], s.data_mode, f"split:{s.id}:{sp.id}", protected,
                                    ctx.duration(cid, "split")))
    return out


def _flow_item(ctx: _Ctx, f: Flow) -> PlanItem:
    w = ctx.workspace(f.workspace)
    cps = []
    for fc in f.checkpoints:
        s = ctx.registry[fc.surface_id] if fc.surface_id in ctx.registry else Surface(fc.surface_id, f.tier)
        cps.append(ctx.checkpoint(fc.checkpoint_id, s, "flow", w, fc.data_mode, persona=f.persona,
                                  flow_id=f.flow_id, step_id=fc.step_id))
    mode = "masked_live" if any(c.data_mode == "masked_live" for c in f.checkpoints) else "frozen"
    protected = bool(set(f.surfaces) & ctx.protected_surfaces)
    return ctx.item(f.flow_id, "flow", f.tier, cps, mode, f"fixture:{f.fixture or f.flow_id}", protected,
                    ctx.duration(f.flow_id, "flow"))


def _flow_primitives(f: Flow, registry: Registry) -> set[str]:
    """A flow's primitives: the registry primitives of its surfaces (4.7 carries none of its own)."""
    return {p for sid in f.surfaces if sid in registry for p in registry[sid].primitives}


def _soak_items(ctx: _Ctx, soak: SoakPlan) -> list[PlanItem]:
    w = ctx.workspace(soak.workspace_id)
    s = ctx.registry[soak.surface_id] if soak.surface_id in ctx.registry else Surface(soak.surface_id, 1)
    out = []
    for n in range(1, SOAK_CYCLES + 1):
        cid = f"flow/{soak.surface_id}/soak/c{n:02d}"
        cp = ctx.checkpoint(cid, s, "flow", w, s.data_mode, base=False, flow_id="soak", step_id=f"c{n:02d}")
        out.append(ctx.item(cid, "soak", 1, [cp], s.data_mode, "soak", True, SOAK_CYCLE_S))
    return out


def _common_filters(items: list[PlanItem], ctx: _Ctx, flows: Mapping[str, Flow], entitlements: frozenset[str],
                    quarantine: frozenset[str]) -> tuple[list[PlanItem], list[Cut]]:
    """`common_filters` of 2.11: entitlement first, then quarantine."""
    kept, cuts = [], []
    for it in items:
        if it.kind == "flow":
            needs = flows[it.item_id].requires
        elif it.kind == "soak":
            needs = frozenset()
        else:
            needs = ctx.registry[it.checkpoints[0].surface_id].entitlements
        if not needs <= entitlements:
            cuts.extend(Cut("entitlement", cp.surface_id, cp.tier, cp) for cp in it.checkpoints)
            continue
        if it.item_id in quarantine:
            cuts.extend(Cut("quarantine", cp.surface_id, cp.tier, cp) for cp in it.checkpoints)
            continue
        q = [cp for cp in it.checkpoints if cp.checkpoint_id in quarantine]
        cuts.extend(Cut("quarantine", cp.surface_id, cp.tier, cp) for cp in q)
        it.checkpoints = [cp for cp in it.checkpoints if cp.checkpoint_id not in quarantine]
        if it.checkpoints:
            kept.append(it)
    return kept, cuts


def _rig_pairs(rigs: Sequence[Rig]) -> list[tuple[str, str]]:
    """Lockstep pairs: consecutive rigs of one fingerprint (3.1, 11.3)."""
    groups: dict[str, list[str]] = {}
    for r in rigs:
        groups.setdefault(r.fingerprint_id, []).append(r.id)
    return [(g[i], g[i + 1]) for g in groups.values() for i in range(0, len(g) - 1, 2)]


def _order_and_cut(items: list[PlanItem], rigs: Sequence[Rig], soak_rig: Rig | None, budget_s: float,
                   explorer_rig: str | None = None, base_reuse: BaseReuse | None = None
                   ) -> tuple[list[PlanItem], dict[str, list[PlanItem]], list[Cut], dict[str, float]]:
    """`order_and_cut` of 2.11: sort, shard, then cut the last item of the highest tier on an over-budget rig."""
    if not rigs:
        raise ScopeConfigError("a run needs at least one rig")
    # L-07, L-08 (1.9, 11.2, 11.3): the soak rig carries only the soak and the Explorer rig no
    # scripted work, so neither is in the sharding pool.
    pool = [r for r in rigs if r.id != explorer_rig and (soak_rig is None or r.id != soak_rig.id)]
    if not pool and any(it.kind != "soak" for it in items):
        raise ScopeConfigError("a run needs at least one rig for scripted work besides the soak and Explorer rigs")
    fingerprint = {r.id: r.fingerprint_id for r in pool}
    items = sorted(items, key=lambda it: (it.tier, KIND_ORDER[it.kind], -it.estimated_s, it.order))
    pairs = _rig_pairs(pool)
    cuts: list[Cut] = []
    placed: list[PlanItem] = []
    for it in items:
        if it.kind == "soak":
            it.rigs = (soak_rig.id,) if soak_rig else (pool[0].id,)
        elif it.data_mode == "masked_live":
            if not pairs:
                cuts.extend(Cut("budget", cp.surface_id, cp.tier, cp) for cp in it.checkpoints)
                continue
            it.rigs = pairs[_stable_hash(it.shard_key) % len(pairs)]
        else:
            it.rigs = (pool[_stable_hash(it.shard_key) % len(pool)].id,)
        if base_reuse is not None and it.kind != "soak":
            # L-09, 3.1: a frozen base is reused only under the fingerprint of the rig the item runs on.
            fp = fingerprint[it.rigs[0]]
            it.checkpoints = [replace(cp, base_source_run_id=base_reuse.source_for(cp.checkpoint_id, fp))
                              if cp.data_mode == "frozen" else cp for cp in it.checkpoints]
        placed.append(it)

    rig_ids = [r.id for r in rigs] + ([soak_rig.id] if soak_rig and soak_rig.id not in {r.id for r in rigs} else [])
    load = {r: 0.0 for r in rig_ids}
    for it in placed:
        for r in it.rigs:
            load[r] += it.rig_cost_s
    removed: set[int] = set()
    while True:
        victim = None
        for r in rig_ids:
            if load[r] <= budget_s:
                continue
            cand = [it for it in placed if r in it.rigs and not it.protected and id(it) not in removed]
            if cand:
                top = max(it.tier for it in cand)
                victim = [it for it in cand if it.tier == top][-1]
                break
        if victim is None:
            break
        removed.add(id(victim))
        for r in victim.rigs:
            load[r] -= victim.rig_cost_s
        cuts.extend(Cut("budget", cp.surface_id, cp.tier, cp) for cp in victim.checkpoints)
    kept = [it for it in placed if id(it) not in removed]
    by_rig = {r: [it for it in kept if r in it.rigs] for r in rig_ids}
    overrun = {r: load[r] - budget_s for r in rig_ids if load[r] > budget_s}
    return kept, by_rig, cuts, overrun


def _attribution(registry: Registry, dep_map: DependencyMap, manifest: Manifest | None,
                 impact: StaticImpact | None) -> dict[str, Attribution]:
    """The primitive and manifest entry each touched surface's findings carry (1.11, 4.13)."""
    out: dict[str, Attribution] = {}
    touched = (impact.touched_primitives if impact else manifest.primitives if manifest else frozenset())
    ordered = sorted(touched, key=lambda p: (dep_map.primitives[p].is_global if p in dep_map.primitives else False, p))
    for p in ordered:
        entry = manifest.entry_touching(p) if manifest else None
        for sid in sorted(dep_map.surfaces_of(p, registry)):
            out.setdefault(sid, Attribution(p, entry.entry_id if entry else None))
    if impact:
        for sid, path in impact.paths.items():
            prims = sorted(dep_map.owning_primitives(path[0]))
            p = prims[0] if prims else None
            entry = manifest.entry_touching(p) if manifest and p else None
            out.setdefault(sid, Attribution(p, entry.entry_id if entry else None))
    if manifest:
        for sid in manifest.surfaces:
            entry = manifest.entry_declaring(sid)
            out[sid] = Attribution(out[sid].primitive if sid in out else None, entry.entry_id if entry else None)
    return out


class _Findings:
    def __init__(self, run_id: str, tag: str):
        self.run_id, self.tag, self.items = run_id, tag, []

    def add(self, kind: str, description: str, **kw) -> None:
        n = len(self.items) + 1
        self.items.append(Finding(finding_id=f"fnd-{self.run_id}-{self.tag}-{n:03d}", run_id=self.run_id, kind=kind,
                                  description=description, first_seen_run_id=self.run_id, **kw))


def _gap_findings(fs: _Findings, impact: StaticImpact, registry: Registry) -> None:
    for g in impact.gaps:
        tier = registry[g.surface].tier if g.surface else None
        if g.reason == "unowned_root":
            fs.add("dependency_map_gap", f"Changed class {g.unowned_root} is in no primitive's and no surface's roots "
                   "(unowned root); the primitive vocabulary needs it (2.10.2)",
                   evidence={"raw_values": {"reason": "unowned_root", "unowned_root": g.unowned_root}})
        elif g.primitive:
            fs.add("dependency_map_gap", f"Static impact reaches {g.surface} from {g.primitive}; the Dependency Map "
                   f"has no edge {g.primitive} -> {g.surface}; a map PR proposes it (2.10.2)",
                   tier=tier, primitive=g.primitive,
                   evidence={"raw_values": {"reason": "missing_edge", "primitive": g.primitive, "surface": g.surface,
                                            "evidence": g.evidence}})
        else:
            fs.add("dependency_map_gap", f"Changed class {g.unowned_root} reaches {g.surface} but no primitive owns it "
                   "(unowned root of the missing edge) (2.10.2)", tier=tier,
                   evidence={"raw_values": {"reason": "missing_edge", "unowned_root": g.unowned_root,
                                            "surface": g.surface, "evidence": g.evidence}})


def compute_scope(run_type: str, manifest: Manifest | None, dep_map: DependencyMap, registry: Registry,
                  dep_index: DependencyIndex | None = None, changed_classes: Iterable[str] = (), *,
                  run_id: str, workspaces: Iterable[Workspace], flows: Iterable[Flow] = (),
                  entitlements: Iterable[str] = (), quarantine: Iterable[str] = (), rigs: Sequence[Rig] = (),
                  durations: Mapping[str, float] | None = None, budget_s: float | None = None,
                  base_reuse: BaseReuse | None = None, candidate_build_id: str | None = None,
                  max_depth: int = STATIC_MAX_DEPTH, max_client: tuple[int, int] = (1904, 1000),
                  corpus_sizes: Mapping[str, Sequence[tuple[int, int]]] | None = None,
                  soak: SoakPlan | None = None, explorer_rig: str | None = None,
                  map_scan_failed: bool = False) -> ScopePlan:
    """The scope of a PR, merge or RC run (2.11).

    `entitlements` is the profile E of the run's logins, `quarantine` the checkpoint and flow ids
    of `quarantine.yaml` (Q), `durations` per-side seconds by checkpoint id (flows by flow id).
    A PR manifest naming another candidate is treated as missing (L-MAN-09); merge and RC runs
    take the union manifest, `Manifest.union`, which is never absent (4.3). `soak` and `explorer_rig` apply to RC runs, and
    `map_scan_failed` to merge runs (2.10.1). The soak and Explorer rigs never take scripted work (11.3).
    """
    if run_type in ("nightly", "calibration"):
        raise ValueError(f"{run_type} runs compute no scope; they take the canary and calibration lists (2.11)")
    if run_type not in BUDGET_S:
        raise ValueError(f"unknown run type {run_type!r}")
    wmap = {w.id: w for w in workspaces}
    flow_map = {f.flow_id: f for f in flows}
    E, Q = frozenset(entitlements), frozenset(quarantine)
    fs = _Findings(run_id, "scope")
    smoke = registry.smoke_subset()

    if run_type == "pr":
        if manifest is not None and candidate_build_id and manifest.candidate != candidate_build_id:
            manifest = None
    elif manifest is None:
        raise ScopeConfigError("merge and RC runs never lack a manifest: pass the union, empty when no PR "
                               "manifests are in range (4.3)")

    impact = None
    if run_type == "pr":
        impact = static_impact(changed_classes, dep_map, registry, dep_index, manifest, max_depth)
        declared = frozenset(s for s in (manifest.surfaces if manifest else ()) if s in registry)
        scope_surfaces = declared | impact.map_expanded | impact.static_surfaces
        census_surfaces = scope_surfaces | smoke
        if manifest is None:
            if impact.static_surfaces or impact.map_expanded or not impact.index_present:
                manifest_state = "no_manifest_ui"
                derived = ",".join(sorted(impact.static_surfaces | impact.map_expanded))
                fs.add("manifest_missing", "No manifest entry; all diffs are reported as collateral until you add one (4.3)",
                       tier=1, evidence={"raw_values": {"derived_surfaces": derived}})
            else:
                manifest_state = "no_manifest_nonui"
        else:
            manifest_state = "loaded"
        if impact.index_present:
            _gap_findings(fs, impact, registry)
        else:
            fs.add("dependency_map_gap", "The candidate's dependency index is missing; the static impact step was "
                   "skipped (2.11)", evidence={"raw_values": {"reason": "no_dependency_index"}})
    else:
        manifest_state = "loaded"
        declared = frozenset(s for s in manifest.surfaces if s in registry)
        scope_surfaces = census_surfaces = frozenset(registry.surfaces)
        if map_scan_failed and run_type == "merge":
            fs.add("dependency_map_gap", "The dependency scan failed; the previous committed map stays in force (2.10.1)",
                   evidence={"raw_values": {"reason": "scan_failed"}})

    ctx = _Ctx(run_type, registry, wmap, declared | smoke, durations or {}, base_reuse, max_client,
               corpus_sizes or {})
    items: list[PlanItem] = []
    cuts: list[Cut] = []
    if run_type == "pr":
        for s in registry:
            if s.id not in census_surfaces:
                cuts.append(Cut("scope", s.id, s.tier))
                continue
            if s.census_enabled:
                if not s.census_workspaces:
                    raise ScopeConfigError(f"{s.id} has census enabled and no workspaces (L-SRF-10)")
                items += _census_items(ctx, s, s.census_workspaces if s.id in declared else s.census_workspaces[:1])
            if s.id in scope_surfaces:
                items += _sweep_items(ctx, s)
        touched = impact.touched_primitives
        selected = [f for f in flow_map.values() if f.tier == 1 and
                    (set(f.surfaces) & scope_surfaces or _flow_primitives(f, registry) & touched)]
    else:
        tiers = ("A",) if run_type == "merge" else ("A", "B")
        pass_ws = sorted(w.id for w in wmap.values() if w.tier in tiers)
        for s in registry:
            if s.census_enabled:
                for wid in pass_ws:
                    w = wmap[wid]
                    if s.id in w.gadgets_present or wid in s.census_workspaces or s.is_global:
                        items += _census_items(ctx, s, [wid])
                    else:
                        cp = ctx.checkpoint(f"census/{s.id}/{wid}", s, "census", w, s.data_mode)
                        cuts.append(Cut("scope", s.id, s.tier, cp))
            items += _sweep_items(ctx, s)
        selected = [f for f in flow_map.values() if run_type == "rc" or f.tier == 1]
    items += [_flow_item(ctx, f) for f in sorted(selected, key=lambda f: f.flow_id)]
    if run_type == "rc" and soak is not None:
        items += _soak_items(ctx, soak)

    items, filtered = _common_filters(items, ctx, flow_map, E, Q)
    budget = float(budget_s if budget_s is not None else BUDGET_S[run_type])
    kept, by_rig, budget_cuts, overrun = _order_and_cut(items, rigs, soak.rig if soak and run_type == "rc" else None, budget,
                                                        explorer_rig, base_reuse)

    proposed = []
    if impact is not None:
        for g in impact.gaps:
            if g.primitive and g.surface:
                proposed.append(Edge(g.primitive, g.surface, "static_analysis",
                                     1.0 if candidate_build_id else 0.9, bool(candidate_build_id),
                                     candidate_build_id, g.evidence))
    return ScopePlan(run_id=run_id, run_type=run_type, budget_s=budget, manifest_state=manifest_state,
                     declared=declared, scope_surfaces=scope_surfaces, census_surfaces=census_surfaces,
                     items=kept, by_rig=by_rig, cuts=cuts + filtered + budget_cuts, findings=fs.items,
                     projected_overrun_s=overrun, attribution=_attribution(registry, dep_map, manifest, impact),
                     proposed_edges=proposed, impact=impact,
                     explorer_rig=explorer_rig if run_type == "rc" else None)


# -- merge run: findings the PR runs missed (1.8, 9.4) ----------------------------------------

def merge_run_findings(run_id: str, findings: Iterable[Finding], prs: Iterable[PrRunSummary], registry: Registry,
                       dep_index: DependencyIndex | None, max_depth: int = STATIC_MAX_DEPTH) -> list[Finding]:
    """`missed_in_pr_run` and `dependency_map_gap` for merge-run findings against each merged PR's run (1.8).

    "Missed in PR run": the surface was in the PR's change-derived scope and the PR run reported
    nothing there. "Missed by static impact" (`detail: static_impact`): the PR run had the surface
    `not_run (reason: scope)` and the merge candidate's dependency index reaches it from a class the
    PR changed. A finding outside the PR's scope on a surface consuming a primitive the PR touched
    is a `dependency_map_gap`.
    """
    prs = list(prs)
    reach = {pr.run_id: (dep_index.reach(pr.changed_classes, max_depth) if dep_index else {}) for pr in prs}
    out = _Findings(run_id, "merge")
    for f in findings:
        if f.kind not in MISSABLE_KINDS or f.checkpoint_id is None:
            continue
        sid = f.checkpoint_id.split("/")[1]
        if sid not in registry:
            continue
        s = registry[sid]
        for pr in prs:
            raw = {"merge_finding": f.finding_id, "pr_run_id": pr.run_id}
            if sid in pr.change_scope and sid not in pr.reported_surfaces:
                out.add("missed_in_pr_run", f"missed in PR run: {pr.run_id} had {sid} in scope and did not report it",
                        checkpoint_id=f.checkpoint_id, tier=f.tier, evidence={"raw_values": raw})
            if sid not in pr.census_surfaces:
                if any(class_matches(cls, r) for paths in reach[pr.run_id].values() for cls in paths for r in s.roots):
                    out.add("missed_in_pr_run", f"missed by static impact: {pr.run_id} had {sid} not_run (scope) and "
                            "the dependency index reaches it from a class the PR changed",
                            checkpoint_id=f.checkpoint_id, tier=f.tier, detail="static_impact",
                            evidence={"raw_values": {**raw, "detail": "static_impact"}})
            consumed = sorted(set(s.primitives) & pr.touched_primitives)
            if sid not in pr.change_scope and consumed:
                out.add("dependency_map_gap", f"{sid} consumes {consumed[0]}, which {pr.run_id} touched, but was "
                        "outside that PR run's scope (1.8)", checkpoint_id=f.checkpoint_id, tier=f.tier,
                        primitive=consumed[0],
                        evidence={"raw_values": {**raw, "reason": "missing_edge", "primitive": consumed[0], "surface": sid}})
    return out.items
