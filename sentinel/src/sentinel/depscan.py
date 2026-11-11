"""The Dependency Map derivation job: the bytecode scan, the edges it proposes and the map update it may make.

Spec: 2.10.1 (the map is derived from a bytecode scan of the build's jars, regenerated on every merge to main; a
failed scan keeps the previous map and opens `dependency_map_gap (scan_failed)` on the next merge run), 2.10.2 (the
static impact step proposes a map PR), 4.4 (the only changes a scan may make, and L-DEP-03), 1.7 T+0:02.

- `parse_jdeps(text)` reads `jdeps -verbose:class` output into the forward dependencies class -> classes it depends
  on (the input of `scope.DependencyIndex.from_depends_on`).
- `derive(forward, dep_map, registry)` resolves every primitive's `source_packages` and every surface's `roots` to the
  scanned classes and returns a `Scan`: the class index and one `DerivedEdge` per (primitive, surface) where a class of
  the surface transitively depends on a class of the primitive.
- `map_update(dep_map, scan, build_id, now)` turns derived edges (a `Scan`, or a PR plan's `scope.ScopePlan.
  proposed_edges`) into a `MapUpdate`: new edges `source: static_analysis, confirmed_from_source: true,
  confirmed_build, evidence, confidence 1.0, first_seen = last_verified = now`, and confirmations of existing edges of
  either source (`confirmed_from_source: true`, `confirmed_build`, confidence raised to 1.0, `last_verified = now`).
  It never removes an edge and never lowers a confidence; `check_update` asserts that, and L-DEP-03 and L-DEP-04 on
  the result, before anything is written (`MapUpdateError`).
- `main(argv)` is `sentinel depmap`: scan, update, write the proposed map and its patch, and a status file whose
  `scan_failed` the next merge run reads (`last_scan_failed`, `compute_scope(map_scan_failed=...)`).

Choices where the spec is ambiguous (reported):

1. Formats read: the JDK 9+ one-line form `<class> -> <class> <location>`, the JDK 8 grouped form (`<class> (<jar>)`
   then indented `-> <class>` lines) and `-dotoutput` lines (`"<class>" -> "<class> (<jar>)";`). Archive summary
   lines (`app.jar -> java.base`, unindented) and warnings are skipped; output with no class dependency at all is a
   failed scan.
2. "Transitively depend" (2.10.1) is unbounded by default (`max_depth=None`); the PR static impact step keeps its
   depth 6 (2.10.2). Global primitives get no derived edges: their declared `"*"` edge covers every surface (4.4).
3. Evidence is the shortest dependency chain from a class of the surface to a class of the primitive, written with
   jdeps's arrow (`A -> B` reads "A depends on B"); ties go to the lexically first chain.
4. An existing edge covers a derived (primitive, surface) when its `surface` is that id, a glob matching it, or
   `"*"` (4.4); such an edge is confirmed, never duplicated by a narrower one.
5. `first_scan_at` (the L-DEP-07 clock) is set to the update's date when the map has none.
6. The status file is JSON `{"status": "ok" | "scan_failed", "build_id", "error"}`; a missing file reads as no failure.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .scope import DependencyIndex, DependencyMap, Edge, Registry, class_matches

MAP_PATH = "registry/dependency-map.yaml"
_CLASS = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
_LINE = re.compile(r"^\s+(\S+)\s+->\s+(\S+)")           # JDK 9+: indented "<class> -> <class> <location>"
_GROUP = re.compile(r"^\s+(\S+)\s+\([^)]*\)\s*$")        # JDK 8: indented "<class> (<archive>)"
_CONT = re.compile(r"^\s+->\s+(\S+)")                    # JDK 8: "   -> <class> <location>"
_DOT = re.compile(r'^\s*"([^"]+)"\s*->\s*"([^"]+)"')     # -dotoutput


class ScanError(ValueError):
    """The scan produced nothing usable (2.10.1 "jar unreadable, scanner crash"): the previous map stays in force."""


class MapUpdateError(ValueError):
    """A map update that would break 4.4's rules (remove an edge, lower a confidence, fail L-DEP-03 or L-DEP-04)."""


def _cls(token: str) -> str | None:
    name = token.strip('"').split(" ", 1)[0]
    return name if _CLASS.match(name) else None


def parse_jdeps(text: str) -> dict[str, frozenset[str]]:
    """The forward class dependencies of `jdeps -verbose:class` output (choice 1): class -> classes it depends on."""
    out: dict[str, set[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if not line.strip():
            continue
        pair: tuple[str | None, str | None] | None = None
        if m := _DOT.match(line):
            pair = (_cls(m.group(1)), _cls(m.group(2)))
        elif m := _CONT.match(line):
            pair = (current, _cls(m.group(1)))
        elif m := _GROUP.match(line):
            current = _cls(m.group(1))
            continue
        elif m := _LINE.match(line):
            pair = (_cls(m.group(1)), _cls(m.group(2)))
        if pair and pair[0] and pair[1] and pair[0] != pair[1]:
            out.setdefault(pair[0], set()).add(pair[1])
    if not out:
        raise ScanError("the scan output holds no class dependency (2.10.1)")
    return {k: frozenset(v) for k, v in out.items()}


@dataclass(frozen=True)
class DerivedEdge:
    """A (primitive, surface) dependency the scan saw, with its evidence chain (choice 3)."""
    primitive: str
    surface: str
    evidence: str


@dataclass(frozen=True)
class Scan:
    """One build's scan: its class index and the edges it derives (2.10.1)."""
    index: DependencyIndex
    edges: tuple[DerivedEdge, ...]
    classes: int


def _closure(index: DependencyIndex, seeds: Sequence[str], max_depth: int | None) -> dict[str, tuple[str, ...]]:
    """Multi-source reverse closure: every class that depends on a seed, with its shortest chain from a seed."""
    paths = {c: (c,) for c in seeds}
    frontier = list(seeds)
    depth = 0
    while frontier and (max_depth is None or depth < max_depth):
        nxt = []
        for x in frontier:
            for d in sorted(index.dependents.get(x, ())):
                if d not in paths:
                    paths[d] = paths[x] + (d,)
                    nxt.append(d)
        frontier, depth = nxt, depth + 1
    return paths


def derive(forward: Mapping[str, Iterable[str]], dep_map: DependencyMap, registry: Registry,
           max_depth: int | None = None) -> Scan:
    """The edges the scan supports (2.10.1): (p, s) when a class under a root of s transitively depends on a class
    under a `source_packages` glob of p (choice 2)."""
    index = DependencyIndex.from_depends_on(forward)
    classes = sorted(set(forward) | {d for ds in forward.values() for d in ds})
    edges: list[DerivedEdge] = []
    for p in sorted(dep_map.primitives.values(), key=lambda x: x.id):
        if p.is_global:
            continue
        seeds = [c for c in classes if any(class_matches(c, g) for g in p.source_packages)]
        if not seeds:
            continue
        paths = _closure(index, seeds, max_depth)
        for s in registry:
            hits = [path for c, path in paths.items() if len(path) > 1 and any(class_matches(c, r) for r in s.roots)]
            if hits:
                best = min(hits, key=lambda path: (len(path), path))
                edges.append(DerivedEdge(p.id, s.id, " -> ".join(reversed(best))))
    return Scan(index, tuple(edges), len(classes))


def _stamp(now: datetime | str) -> str:
    return now if isinstance(now, str) else now.isoformat()


def _covers(edge: Edge, primitive: str, surface: str) -> bool:
    return edge.primitive == primitive and (edge.surface in (surface, "*") or fnmatch.fnmatchcase(surface, edge.surface))


@dataclass(frozen=True)
class MapUpdate:
    """The map a scan proposes (4.4): every old edge, the confirmed ones updated, the new ones appended."""
    build_id: str
    edges: tuple[Edge, ...]
    added: tuple[Edge, ...]
    confirmed: tuple[Edge, ...]
    first_scan_at: str | None = None

    @property
    def empty(self) -> bool:
        """Nothing to propose: no new edge and no confirmation."""
        return not self.added and not self.confirmed

    def document(self, doc: Mapping[str, Any]) -> dict:
        """The proposed `registry/dependency-map.yaml`: `doc` with the new edge list (and `first_scan_at`)."""
        out = dict(doc.get("dependency_map", doc) if isinstance(doc.get("dependency_map"), Mapping) else doc)
        if self.first_scan_at and not out.get("first_scan_at"):
            out["first_scan_at"] = self.first_scan_at
        out["edges"] = [e.to_dict() for e in self.edges]
        return out

    def patch(self) -> dict:
        """The PR body's patch: the edges added and the edges confirmed, with the build that saw them."""
        return {"build": self.build_id, "add": [e.to_dict() for e in self.added],
                "confirm": [e.to_dict() for e in self.confirmed]}


def check_update(before: Sequence[Edge], after: Sequence[Edge]) -> list[str]:
    """The 4.4 invariant on a proposed map: no edge removed, no confidence lowered, L-DEP-03 and L-DEP-04 on every
    edge, `first_seen` and `last_verified` on every edge the update wrote."""
    problems: list[str] = []
    seen: dict[tuple[str, str], Edge] = {}
    for e in after:
        if (e.primitive, e.surface) in seen:
            problems.append(f"duplicate edge {e.primitive} -> {e.surface} (L-DEP-04)")
        seen[(e.primitive, e.surface)] = e
    old = {(e.primitive, e.surface): e for e in before}
    for key, e in old.items():
        new = seen.get(key)
        if new is None:
            problems.append(f"edge {key[0]} -> {key[1]} removed; a scan never removes an edge (4.4)")
        elif new.confidence < e.confidence:
            problems.append(f"edge {key[0]} -> {key[1]} confidence lowered from {e.confidence} to {new.confidence} (4.4)")
    for key, e in seen.items():
        name = f"edge {key[0]} -> {key[1]}"
        if e.source == "declared" and e.confidence != 1.0:
            problems.append(f"{name}: a declared edge has confidence 1.0 (L-DEP-03)")
        if e.source == "static_analysis":
            if not e.evidence:
                problems.append(f"{name}: a static_analysis edge carries evidence (L-DEP-03)")
            if not 0 < e.confidence <= 1:
                problems.append(f"{name}: confidence {e.confidence} is outside (0, 1] (L-DEP-03)")
            if e.confidence == 1.0 and not e.confirmed_from_source:
                problems.append(f"{name}: confidence 1.0 needs confirmed_from_source (L-DEP-03)")
        if e.confirmed_from_source and not e.confirmed_build:
            problems.append(f"{name}: confirmed_from_source needs confirmed_build (L-DEP-03)")
        if (key not in old or old[key] != e) and not (e.first_seen and e.last_verified):
            problems.append(f"{name}: first_seen and last_verified are required (4.4)")
    return problems


def map_update(dep_map: DependencyMap, scan: Scan | Iterable[DerivedEdge | Edge], build_id: str,
               now: datetime | str) -> MapUpdate:
    """Add or confirm only (4.4, module docstring). `scan` is a `Scan` (the merge-to-main job) or derived or proposed
    edges (a PR run's `ScopePlan.proposed_edges`, the static impact step of 2.10.2)."""
    if not build_id:
        raise MapUpdateError("a scan update names the build whose scan saw the edges (4.4 confirmed_build)")
    stamp = _stamp(now)
    derived = scan.edges if isinstance(scan, Scan) else tuple(scan)
    edges = list(dep_map.edges)
    confirmed: dict[int, Edge] = {}
    added: list[Edge] = []
    for d in derived:
        if not d.evidence:
            raise MapUpdateError(f"derived edge {d.primitive} -> {d.surface} has no evidence (L-DEP-03)")
        hit = next((i for i, e in enumerate(edges) if _covers(e, d.primitive, d.surface)), None)
        if hit is None:
            new = Edge(d.primitive, d.surface, "static_analysis", 1.0, True, build_id, d.evidence,
                       first_seen=stamp, last_verified=stamp)
            edges.append(new)
            added.append(new)
        elif hit not in confirmed:
            e = edges[hit]
            confirmed[hit] = replace(e, confirmed_from_source=True, confirmed_build=build_id,
                                     confidence=max(e.confidence, 1.0), last_verified=stamp,
                                     first_seen=e.first_seen or stamp,
                                     evidence=e.evidence or (d.evidence if e.source == "static_analysis" else None))
            edges[hit] = confirmed[hit]
    problems = check_update(dep_map.edges, edges)
    if problems:
        raise MapUpdateError("; ".join(problems))
    first = (stamp[:10] if isinstance(now, str) else now.date().isoformat()) if derived else None
    return MapUpdate(build_id, tuple(edges), tuple(added), tuple(confirmed[i] for i in sorted(confirmed)), first)


def last_scan_failed(status_path: str | Path) -> bool:
    """True when the last `sentinel depmap` run recorded `scan_failed` (choice 6): the next merge run passes it to
    `compute_scope(map_scan_failed=True)`, which opens `dependency_map_gap (scan_failed)` (2.10.1)."""
    p = Path(status_path)
    if not p.exists():
        return False
    return json.loads(p.read_text(encoding="utf-8")).get("status") == "scan_failed"


def _load_registry(config: Path) -> Registry:
    from .lint.repo import Repo
    return Registry.from_entries(d.data for d in Repo.load(config).of("surface"))


def _yaml_safe(x: Any) -> Any:
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    if isinstance(x, Mapping):
        return {k: _yaml_safe(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_yaml_safe(v) for v in x]
    return x


def main(argv: Sequence[str] | None = None) -> int:
    """`sentinel depmap`: scan, derive, update (module docstring). Exit 0 with the proposed map, 2 on a failed scan."""
    ap = argparse.ArgumentParser(prog="sentinel depmap", description="Derive the Dependency Map from a bytecode scan "
                                 "(2.10.1) and write the proposed map and its patch (4.4).")
    ap.add_argument("--jdeps", required=True, help="jdeps -verbose:class output for the build's jars")
    ap.add_argument("--config", required=True, help="the sentinel-config checkout (registry and map)")
    ap.add_argument("--build", required=True, help="the build id the scan read (confirmed_build)")
    ap.add_argument("--now", required=True, help="the update's timestamp (first_seen, last_verified)")
    ap.add_argument("--out", help="where to write the proposed dependency-map.yaml")
    ap.add_argument("--patch", help="where to write the patch (edges added and confirmed) for the map PR")
    ap.add_argument("--status", help="where to write the scan status the next merge run reads")
    ap.add_argument("--max-depth", type=int, default=None, help="bound the transitive closure (default: none)")
    args = ap.parse_args(argv)

    def status(state: str, error: str | None = None) -> None:
        if args.status:
            Path(args.status).write_text(json.dumps({"status": state, "build_id": args.build, "error": error},
                                                    sort_keys=True) + "\n", encoding="utf-8")

    config = Path(args.config)
    try:
        doc = yaml.safe_load((config / MAP_PATH).read_text(encoding="utf-8"))
        dep_map = DependencyMap.from_dict(doc)
        scan = derive(parse_jdeps(Path(args.jdeps).read_text(encoding="utf-8")), dep_map, _load_registry(config),
                      args.max_depth)
    except (OSError, ScanError, yaml.YAMLError, KeyError, TypeError) as exc:
        status("scan_failed", str(exc))
        print(f"scan failed; the committed map stays in force (2.10.1): {exc}", file=sys.stderr)
        return 2
    try:
        update = map_update(dep_map, scan, args.build, args.now)
    except MapUpdateError as exc:
        status("scan_failed", str(exc))
        print(f"map update refused (4.4): {exc}", file=sys.stderr)
        return 2
    status("ok")
    if args.out:
        Path(args.out).write_text(yaml.safe_dump(_yaml_safe(update.document(doc)), sort_keys=False), encoding="utf-8")
    if args.patch:
        Path(args.patch).write_text(yaml.safe_dump(update.patch(), sort_keys=False), encoding="utf-8")
    print(f"{len(update.added)} edges added, {len(update.confirmed)} confirmed from {scan.classes} classes "
          f"of build {args.build}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
