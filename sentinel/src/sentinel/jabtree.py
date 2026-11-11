"""The accessible-tree dump and its build-to-build diff (DR-01 Branch A; 2.8.1 JAB values, 2.13 B09, 4.13
`jab_tree_changed`, 11.8).

At build intake the rig dumps the main frame's accessible tree through `ports.AccessPort` (`dump_from_port`); the dump
is compared with the previous (approved) build's (`diff`):

- `TreeDump`: the tree as `(path, role, name)` entries in depth-first order, with its `sha256` (the tree hash of 11.8)
  and a JSON-lines form to store per build (`to_jsonl`, `from_jsonl`).
- `TreeDiff.finding()`: a `jab_tree_changed` finding when the hashes differ (4.13: the run falls back to Branch B
  bounds for the affected surfaces and the registry owner is told; a finding, never an outcome or a `not_comparable`
  reason).
- `TreeDiff.lost`: the accessible names the new build no longer exposes, the B09 inputs the report posts as
  `collateral` ("Branch A diffs the accessible tree build to build and reports lost names as collateral"; nothing
  absolute is asserted).
- `ocr_fallback(cells, current, previous)`: from the same build-to-build comparison, the value cells whose path the
  new tree no longer exposes or whose position it no longer vouches for (choice 6); `TreeJabReader`, the Oracle's
  `JabReader` over the tree, reads nothing for them, so `oracle.sources.extract` falls back to OCR and records
  `oracle_source_fallback` (2.8.1: "a cell the tree no longer exposes falls back to OCR").

Choices (reported):

1. A path is positional: each context is `role[k]`, `k` its index among the siblings of the same role, joined from the
   root with `/` (`frame[0]/panel[1]/table[0]/table cell[13]`). It names value cells, whose accessible name is their
   value and so cannot name them. The tree hash is over the entries' `(path, role, name)` only: states and bounds
   change at run time (focus, selection, layout) and are not the tree's identity.
2. A lost name is counted by `(role, name)` over the whole tree, so a control that moved, or whose ancestors were
   renamed or re-indexed, has lost nothing: the deficit of each named `(role, name)` from the previous build to the
   new one is lost. Which previous entries are reported for a deficit: first those whose path the new tree lacks or
   names differently, then the last in depth-first order. `gained` is the same count the other way. An empty name is
   never a name.
3. A value cell's text is its context's accessible name (Swing table cells expose the rendered text as their
   name); Phase 0 decides per surface whether the bridge exposes values at all (2.8.1 "surfaces where the spike
   showed values"). `TreeJabReader` dumps the tree once per `refresh()`, so a checkpoint's reads see one tree.
4. The finding's detail names the counts and up to `DETAIL_EXAMPLES` lost names; its severity is left to the
   finding routing.
5. A child the bridge did not give (a null child, or one whose info could not be read) is the `UNREAD` placeholder in
   the port's tree (`win32.access_bridge` choice 4). Its role is unknown, so every sibling after it may have moved
   down one index and a positional path could name a neighbouring cell. So a tree with a placeholder has no dump
   (`dump` raises `IncompleteTree`; `dump_from_port` returns None, as for a bridge that does not answer): an
   incomplete walk is never hashed or diffed as the build's tree. `TreeJabReader` reads nothing under a context that
   lost a child (2.8.1: the cell is not exposed this time), so the field falls back to its alternate source with
   `oracle_source_fallback`, or ends `needs_human (oracle_unreadable)` when none reads it; the rest of the tree reads.
6. A value cell's path still existing in the new build does not make it the same cell: a column inserted before it
   moves the cell it names. So a cell falls back to OCR for the run when any context on its path (the root down to
   its parent) has a different sequence of child roles than in the previous build's dump: a sibling was added,
   removed or changed role, and the positional index may name another cell. The whole sequence is compared, not the
   part before the cell, because grid cells are siblings of one role in row order, where an inserted column shows
   only as a longer sequence. Names (the values themselves) are not compared, so a value change alone never turns JAB
   off; a swap of two same-role siblings is not visible in the tree and stays the registry owner's (Phase 0 and the
   `jab_tree_changed` finding).

Verified here: the dump, the hash, the diff and the fallback on synthetic trees, and the fallback through
`oracle.sources.extract`. Only Phase 0 on a rig verifies the real trees' stability from run to run on one build, and
which cells expose values (11.8 DR-01).
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from .canonical import sha256_id
from .oracle.sources import EXACT_CONFIDENCE, Reading
from .orchestrator.ports import AccessNode, AccessPort
from .orchestrator.results import FindingSpec

DETAIL_EXAMPLES = 10
UNREAD_ROLE = "<unread>"                         # no bridge role has angle brackets
UNREAD = AccessNode(UNREAD_ROLE, "", None)       # a child the bridge did not give (choice 5)


class IncompleteTree(ValueError):
    """The walk lost a child (choice 5): the tree has no positional dump."""


@dataclass(frozen=True)
class DumpEntry:
    """One context of a dump: its positional path (choice 1), role and accessible name."""

    path: str
    role: str
    name: str


@dataclass(frozen=True)
class TreeDump:
    """A build's accessible tree in depth-first order."""

    build_id: str
    entries: tuple[DumpEntry, ...]

    @property
    def sha256(self) -> str:
        """The tree hash (11.8): `sha256:` over the canonical JSON of the `(path, role, name)` entries in order."""
        return sha256_id([[e.path, e.role, e.name] for e in self.entries])

    def by_path(self) -> dict[str, DumpEntry]:
        return {e.path: e for e in self.entries}

    def to_jsonl(self) -> str:
        """A header line (build id and hash), then one line per entry."""
        lines = [json.dumps({"build_id": self.build_id, "sha256": self.sha256, "entries": len(self.entries)},
                            ensure_ascii=False)]
        lines += [json.dumps({"path": e.path, "role": e.role, "name": e.name}, ensure_ascii=False)
                  for e in self.entries]
        return "\n".join(lines) + "\n"

    @classmethod
    def from_jsonl(cls, text: str) -> TreeDump:
        """The stored form back; a header whose hash or count does not match its entries raises ValueError."""
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise ValueError("an empty tree dump")
        head = json.loads(lines[0])
        dump = cls(str(head["build_id"]), tuple(DumpEntry(str(d["path"]), str(d["role"]), str(d["name"]))
                                                for d in map(json.loads, lines[1:])))
        if head.get("entries") != len(dump.entries) or head.get("sha256") != dump.sha256:
            raise ValueError(f"tree dump of build {dump.build_id}: header does not match its entries")
        return dump


def _walk(root: AccessNode) -> tuple[list[DumpEntry], list[str]]:
    """The entries of the contexts read (choice 1), and the paths of the contexts that lost a child (choice 5)."""
    out: list[DumpEntry] = []
    gaps: list[str] = []

    def walk(node: AccessNode, path: str) -> None:
        out.append(DumpEntry(path, node.role, node.name))
        seen: Counter[str] = Counter()
        for child in node.children:
            if child.role == UNREAD_ROLE:
                if not gaps or gaps[-1] != path:
                    gaps.append(path)
                continue
            out_path = f"{path}/{child.role}[{seen[child.role]}]"
            seen[child.role] += 1
            walk(child, out_path)

    if root.role == UNREAD_ROLE:
        return [], [""]
    walk(root, f"{root.role}[0]")
    return out, gaps


def dump(root: AccessNode, build_id: str) -> TreeDump:
    """The tree under `root` as a `TreeDump` (choice 1); `IncompleteTree` when the walk lost a child (choice 5)."""
    entries, gaps = _walk(root)
    if gaps:
        raise IncompleteTree(f"the accessible tree walk lost a child of {len(gaps)} context(s), first "
                             f"{gaps[0] or 'the root'}: positional paths after the gap would shift")
    return TreeDump(build_id, tuple(entries))


def dump_from_port(port: AccessPort, hwnd: int, build_id: str) -> TreeDump | None:
    """The main frame's tree through the Access Bridge; None when the bridge is down, does not answer, or the walk
    lost a child (choice 5)."""
    if not port.available():
        return None
    root = port.tree(hwnd)
    if root is None:
        return None
    try:
        return dump(root, build_id)
    except IncompleteTree:
        return None


@dataclass(frozen=True)
class LostName:
    """One accessible name the new build no longer exposes (B09), where the previous build had it."""

    role: str
    name: str
    path: str

    def describe(self) -> str:
        return f"{self.role} {self.name!r} at {self.path}"


@dataclass(frozen=True)
class TreeDiff:
    """The new build's tree against the previous build's."""

    previous: TreeDump
    current: TreeDump
    lost: tuple[LostName, ...]
    gained: tuple[LostName, ...]
    removed_paths: tuple[str, ...]
    added_paths: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.previous.sha256 != self.current.sha256

    def finding(self) -> FindingSpec | None:
        """`jab_tree_changed` when the tree hash differs (4.13, 11.8; choice 4); None when it is the same tree."""
        if not self.changed:
            return None
        examples = "; ".join(x.describe() for x in self.lost[:DETAIL_EXAMPLES])
        more = f" (and {len(self.lost) - DETAIL_EXAMPLES} more)" if len(self.lost) > DETAIL_EXAMPLES else ""
        return FindingSpec(
            "jab_tree_changed",
            f"accessible tree of build {self.current.build_id} differs from build {self.previous.build_id}: "
            f"{len(self.lost)} names lost, {len(self.gained)} gained; Branch B bounds for the affected surfaces "
            "this run (11.8)",
            detail=(f"tree {self.previous.sha256} -> {self.current.sha256}; contexts removed {len(self.removed_paths)}"
                    f", added {len(self.added_paths)}" + (f"; lost: {examples}{more}" if examples else "")))


def _deficit(old: TreeDump, new: TreeDump) -> tuple[LostName, ...]:
    """The named `(role, name)` occurrences of `old` that `new` lacks (choice 2)."""
    have = Counter((e.role, e.name) for e in new.entries if e.name)
    want = Counter((e.role, e.name) for e in old.entries if e.name)
    at = new.by_path()
    out: list[LostName] = []
    for key, count in want.items():
        short = count - have.get(key, 0)
        if short <= 0:
            continue
        cands = [e for e in old.entries if (e.role, e.name) == key]
        moved = [e for e in cands if at.get(e.path) is None or at[e.path].name != e.name]
        pick = (moved + [e for e in reversed(cands) if e not in moved])[:short]
        out += [LostName(e.role, e.name, e.path) for e in pick]
    order = {e.path: i for i, e in enumerate(old.entries)}
    return tuple(sorted(out, key=lambda x: order[x.path]))


def diff(previous: TreeDump, current: TreeDump) -> TreeDiff:
    """Compare two builds' trees (module docstring)."""
    old_paths, new_paths = previous.by_path(), current.by_path()
    return TreeDiff(previous, current, _deficit(previous, current), _deficit(current, previous),
                    tuple(p for p in old_paths if p not in new_paths),
                    tuple(p for p in new_paths if p not in old_paths))


def _child_roles(d: TreeDump) -> dict[str, tuple[str, ...]]:
    """Each context's path -> the roles of its children in order."""
    out: dict[str, list[str]] = {}
    for e in d.entries:
        parent, sep, _ = e.path.rpartition("/")
        if sep:
            out.setdefault(parent, []).append(e.role)
    return {p: tuple(roles) for p, roles in out.items()}


def ocr_fallback(cells: Mapping[str, str], current: TreeDump, previous: TreeDump | None) -> frozenset[str]:
    """The fields (field -> cell path) that read by OCR this run (2.8.1, choice 6): the current tree does not expose
    the cell's path, or, against the previous build's dump, a context on the path has different child roles, so the
    positional index may now name another cell. `previous` None (no earlier build) checks presence only."""
    paths = current.by_path()
    now = _child_roles(current)
    before = _child_roles(previous) if previous is not None else None
    out: set[str] = set()
    for f, p in cells.items():
        if p not in paths:
            out.add(f)
            continue
        if before is None:
            continue
        parts = p.split("/")
        if any(now.get(a, ()) != before.get(a, ()) for a in ("/".join(parts[:i]) for i in range(1, len(parts)))):
            out.add(f)
    return frozenset(out)


class TreeJabReader:
    """`oracle.sources.JabReader` over the accessible tree (choice 3): a field's value is the accessible name of the
    context at its cell path; nothing is read for a field marked lost (`ocr_fallback`), whose cell the current
    tree does not expose, or whose path runs under a context that lost a child this walk (choice 5), so the Oracle
    falls back to OCR with `oracle_source_fallback`."""

    name = "jab"

    def __init__(self, port: AccessPort, hwnd: Callable[[], int | None], cells: Mapping[str, str], *,
                 lost: Iterable[str] = ()):
        self.port = port
        self.hwnd = hwnd
        self.cells = dict(cells)
        self.lost = frozenset(lost)
        self._paths: dict[str, DumpEntry] | None = None

    def refresh(self) -> None:
        """Forget the tree read so far: the next read dumps it again (call at each checkpoint)."""
        self._paths = None

    def read(self, field: str) -> Reading | None:
        path = self.cells.get(field)
        if path is None or field in self.lost:
            return None
        if self._paths is None:
            self._paths = self._read_tree()
        entry = self._paths.get(path)
        return Reading(field, "jab", entry.name, EXACT_CONFIDENCE) if entry is not None else None

    def _read_tree(self) -> dict[str, DumpEntry]:
        """The current tree's entries by path, without those under a context that lost a child (choice 5); empty when
        the bridge is down or does not answer."""
        hwnd = self.hwnd()
        if not hwnd or not self.port.available():
            return {}
        root = self.port.tree(hwnd)
        if root is None:
            return {}
        entries, gaps = _walk(root)
        return {e.path: e for e in entries if not any(e.path.startswith(g + "/") for g in gaps)}
