"""Report clusters of one run (9.7 Clustering).

- Sweep and split checkpoints of one surface and one region with equal category form one
  cluster per run whatever their sizes (one cluster per checkpoint kind, so a sweep and a
  split of one surface stay apart: "findings of one run cluster by checkpoint kind").
- Census and flow checkpoints form one cluster when category, primitive and region are equal,
  the pHash values are within Hamming 6 and the largest diff component boxes are equal within
  4 px. Clustering is transitive (single linkage), so the result does not depend on input order.
- Every `needs_human (reason: global_shift)` checkpoint joins one cluster (9.2 banner, 3.7).

Choices where 9.7 is silent (reported to the coordinator):
- Component boxes are compared as recorded on `Component.bbox`; the Diff Engine is assumed to
  store them relative to the region's anchored bounds, as 9.7 requires for this comparison.
- A member without a pHash, or without a box when the other has one, clusters with nobody.
- The representative member (whose crops, finding links and actions the item shows) is the
  lower median of the unsuppressed members, ordered by step area (sweep), fraction (split) or
  checkpoint id (census, flow); ties break on width, then checkpoint id. For 9.3 this gives
  480x360 of the four height-360 steps and 640x480 of the eight others, as the sample shows,
  and keeps the smallest and largest steps (the Reviewer's two crops) as the extremes.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..records import CheckpointOutcome, Decision, Finding
from ..taxonomy import checkpoint_parts

PHASH_TOLERANCE = 6     # 9.7: cross-checkpoint clustering
BOX_TOLERANCE_PX = 4    # 9.7: component boxes equal within 4 px


@dataclass
class Member:
    """One checkpoint as the report sees it: its outcome, primary finding and display category."""

    outcome: CheckpointOutcome
    finding: Finding | None
    category: str                      # computed category (the decision signature's when suppressed)
    group: int | None                  # Unexpected kind order of 9.2, None outside Unexpected
    region: str | None
    decision_id: str | None = None     # set when an earlier decision suppresses this member
    decision: Decision | None = None

    @property
    def suppressed(self) -> bool:
        """Only an active decision suppresses: a reopened one returns the finding to Unexpected, undecided (9.7)."""
        return self.decision_id is not None and (self.decision is None or self.decision.status == "active")

    @property
    def checkpoint_id(self) -> str:
        return self.outcome.checkpoint_id

    @property
    def kind(self) -> str:
        return self.outcome.test_kind or checkpoint_parts(self.checkpoint_id)["kind"]

    @property
    def size(self) -> tuple[int, int] | None:
        """(w, h) of a sweep step, from the checkpoint id's `<w>x<h>` qualifier (4.1)."""
        if self.kind != "sweep":
            return None
        w, _, h = self.checkpoint_id.rsplit("/", 1)[1].partition("x")
        return int(w), int(h)

    @property
    def host(self) -> str | None:
        parts = self.checkpoint_id.split("/")
        return parts[2] if self.kind in ("sweep", "split") else None

    @property
    def fraction(self) -> float | None:
        return float(self.checkpoint_id.rsplit("/", 1)[1]) if self.kind == "split" else None

    @property
    def pending(self) -> bool:
        return self.outcome.review_state == "pending"

    @property
    def reason_code(self) -> str | None:
        return self.outcome.reason.partition(":")[0] if self.outcome.reason else None

    def largest_box(self) -> tuple[int, int, int, int] | None:
        diff = self.outcome.diff
        if not diff or not diff.components:
            return None
        comps = [c for c in diff.components if self.region is None or c.region_id in (None, self.region)]
        if not comps:
            return None
        return tuple(max(comps, key=lambda c: (c.area, c.bbox)).bbox)


@dataclass
class Cluster:
    """A report item's checkpoints (9.7): one item, one Reviewer call when it needs one."""

    members: list[Member]

    @property
    def kind(self) -> str:
        return self.members[0].kind

    @property
    def unsuppressed(self) -> list[Member]:
        return [m for m in self.members if not m.suppressed]

    @property
    def suppressed(self) -> list[Member]:
        return [m for m in self.members if m.suppressed]

    @property
    def fully_suppressed_one_verdict(self) -> bool:
        """9.7: a cluster is suppressed only when every member matches decisions of one verdict."""
        if self.unsuppressed:
            return False
        verdicts = {m.decision.verdict if m.decision else m.decision_id for m in self.members}
        return len(verdicts) == 1

    @property
    def surfaces(self) -> list[str]:
        seen: list[str] = []
        for m in sorted(self.members, key=lambda m: m.checkpoint_id):
            if m.outcome.surface_id not in seen:
                seen.append(m.outcome.surface_id)
        return seen

    @property
    def representative(self) -> Member:
        pool = self.unsuppressed or self.members
        ordered = sorted(pool, key=_order_key)
        return ordered[(len(ordered) - 1) // 2]

    @property
    def sizes(self) -> list[tuple[int, int]]:
        return sorted({m.size for m in self.members if m.size}, key=lambda s: (s[0] * s[1], s[0]))


def _order_key(m: Member) -> tuple:
    if m.size:
        w, h = m.size
        return (w * h, w, m.checkpoint_id)
    if m.fraction is not None:
        return (m.fraction, 0, m.checkpoint_id)
    return (0, 0, m.checkpoint_id)


def hamming(a: str, b: str) -> int:
    """Bit distance between two 64-bit pHash values written as 16 hex characters (4.13)."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _boxes_match(a: tuple | None, b: tuple | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return all(abs(x - y) <= BOX_TOLERANCE_PX for x, y in zip(a, b))


def _census_match(a: Member, b: Member) -> bool:
    pa, pb = a.finding.phash if a.finding else None, b.finding.phash if b.finding else None
    if not pa or not pb or hamming(pa, pb) > PHASH_TOLERANCE:
        return False
    return _boxes_match(a.largest_box(), b.largest_box())


def _category_key(m: Member) -> tuple:
    reason = m.reason_code if m.category == "needs_human" and not m.suppressed else None
    return (m.category, m.group, reason, m.pending)


def cluster_members(members: list[Member]) -> list[Cluster]:
    """Group members into the clusters of 9.7, in a deterministic order."""
    clusters: dict[tuple, list[Member]] = {}
    census: dict[tuple, list[Member]] = {}
    for m in sorted(members, key=lambda m: m.checkpoint_id):
        if m.category == "needs_human" and m.reason_code == "global_shift" and not m.suppressed:
            clusters.setdefault(("global_shift",), []).append(m)
        elif m.kind in ("sweep", "split"):
            key = (m.kind, m.outcome.surface_id, m.region) + _category_key(m)
            clusters.setdefault(key, []).append(m)
        else:
            key = (m.kind, m.outcome.primitive, m.region) + _category_key(m)
            census.setdefault(key, []).append(m)
    out = [Cluster(ms) for ms in clusters.values()]
    for ms in census.values():
        parent = list(range(len(ms)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(ms)):
            for j in range(i + 1, len(ms)):
                if find(i) != find(j) and _census_match(ms[i], ms[j]):
                    parent[find(j)] = find(i)
        groups: dict[int, list[Member]] = {}
        for i, m in enumerate(ms):
            groups.setdefault(find(i), []).append(m)
        out.extend(Cluster(g) for g in groups.values())
    out.sort(key=lambda c: c.members[0].checkpoint_id)
    return out
