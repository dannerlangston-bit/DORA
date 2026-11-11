"""The shard planner: work units packed onto rigs longest-first, session startups included.

Spec: 11.3 (sharding and parallelism), 2.11 `order_and_cut` (the shard rule, SD-14), 11.2 (session startups and the
wall formula, PA-04), 12.1 Shard, 3.1 and 7.9 (frozen items run base then candidate on one rig, a reused base runs
the candidate only; masked_live items run in lockstep on two identically fingerprinted rigs), 10.6 and 11.2 Nightly
and Calibration rows (A/A passes on one rig).

`plan(items, rigs, p50, startups)` groups the items by `shard_key` (the caller's key: census by surface tab and data
mode, flows by a stable hash of `flow_id` within tier with flows sharing a `state_scope` or a fixture under one key,
a sweep or split ladder by surface and host or splitter), packs the groups onto rigs longest first (LPT: each group,
by measured p50, goes to the rig with the least load so far, a load that counts the session startups and workspace
switches of the work already there), and returns a `ShardLayout`: per rig the items, the sessions it opens and its
load. `ShardLayout.remove` takes an item out (a budget cut) and recomputes its rigs.

Two modes:

- `pair` (PR, merge and RC runs). masked_live groups go to lockstep pairs first, each costing its per-side time and one
  session on both rigs of the pair, onto the pair whose busier rig is least loaded; frozen groups then go to the least
  loaded rig of the whole pool, so the rigs left over take frozen work first and a frozen shard uses a pair rig once it
  is the idlest (11.3). A frozen item costs twice its per-side time when its base cannot be
  reused under the fingerprint of the rig it is placed on, once otherwise; the frozen work on a rig opens one session
  (the candidate) plus a second (the base) when any item there needs a base capture (11.2: "a frozen shard without
  reuse opens two sessions on its rig, 8 min").
- `aa` (Nightly A/A pass, Calibration passes). Every item runs `passes` times back to back on one rig of one build, in
  one session per rig, whatever its data mode: two passes on one rig are never lockstep (10.6).

Choices where the spec is ambiguous (reported):

1. A shard is the work of one data mode on one rig (or one pair): 11.3 makes a shard "all frozen or all masked_live",
   one session per rig, so a rig carrying both opens a frozen and a masked_live shard. Groups keyed by the caller are
   the atomic placement units; a rig's groups of one data mode form its shard.
2. Session startup: `Startups.session_s` (4 min, PA-04) per session, or `fixture_reset_s` (8 min) for every session
   when `fixture_reset` is set, since the 7.6 profile reset runs in each session's gate sequence when it runs at all.
3. Workspace switches: a session loads its first workspace in its startup; each further distinct workspace costs
   `workspace_switch_s` (45 s, PA-04), counted per session (a frozen rig with a base session switches in both).
4. Pairs are consecutive rigs of one fingerprint, in pool order (3.1, 11.3).
5. Ties in LPT go to the group with the smaller stable hash of its key, then to the rig (or pair) earlier in the pool,
   so the plan is deterministic.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

SESSION_STARTUP_S = 4 * 60.0            # PA-04: launch through the canary flow
FIXTURE_RESET_STARTUP_S = 8 * 60.0      # PA-04: a session that includes a fixture reset (7.6)
WORKSPACE_SWITCH_S = 45.0               # PA-04: workspace switch within a session
MODES = ("pair", "aa")


@dataclass(frozen=True)
class Startups:
    """Session startup and workspace switch costs (PA-04, 11.2); see choices 2 and 3."""
    session_s: float = SESSION_STARTUP_S
    fixture_reset_s: float = FIXTURE_RESET_STARTUP_S
    workspace_switch_s: float = WORKSPACE_SWITCH_S
    fixture_reset: bool = False

    @property
    def per_session_s(self) -> float:
        """The startup of one session the plan opens."""
        return self.fixture_reset_s if self.fixture_reset else self.session_s


class Checkpoint(Protocol):
    """What the planner reads of a planned checkpoint (`scope.PlannedCheckpoint`)."""

    @property
    def checkpoint_id(self) -> str: ...

    @property
    def workspace_id(self) -> str: ...

    @property
    def data_mode(self) -> str: ...


class Item(Protocol):
    """What the planner reads of a schedulable unit (`scope.PlanItem`); `rigs` is written."""
    item_id: str
    kind: str
    tier: int
    data_mode: str
    shard_key: str
    estimated_s: float
    checkpoints: Sequence[Checkpoint]
    rigs: tuple[str, ...]


class RigLike(Protocol):
    """A rig of the pool (`scope.Rig`)."""

    @property
    def id(self) -> str: ...

    @property
    def fingerprint_id(self) -> str: ...


Reuse = Callable[[str, str], bool]        # (checkpoint id, rig fingerprint) -> the stored frozen base is reusable


def stable_hash(key: str) -> int:
    """A process-independent hash (sha256) for keys and tie-breaks."""
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], 16)


def rig_pairs(rigs: Sequence[RigLike]) -> list[tuple[str, str]]:
    """Lockstep pairs: consecutive rigs of one fingerprint, in pool order (choice 4)."""
    groups: dict[str, list[str]] = {}
    for r in rigs:
        groups.setdefault(r.fingerprint_id, []).append(r.id)
    return [(g[i], g[i + 1]) for g in groups.values() for i in range(0, len(g) - 1, 2)]


@dataclass(frozen=True)
class RigCost:
    """One rig's load: capture time, session startups and workspace switches (11.2 wall formula)."""
    capture_s: float
    sessions: int
    startup_s: float
    switch_s: float

    @property
    def total_s(self) -> float:
        return self.capture_s + self.startup_s + self.switch_s

    @property
    def work_s(self) -> float:
        """What the 2.11 budget B (capture time) is compared with: captures plus workspace switches."""
        return self.capture_s + self.switch_s


@dataclass
class Shard:
    """The work of one data mode on one rig, or on both rigs of a lockstep pair (12.1 Shard, choice 1)."""
    rigs: tuple[str, ...]
    data_mode: str                   # frozen | masked_live | any (an A/A shard: one session runs both modes, 10.6)
    items: list
    sessions: int                    # per rig: 2 for a frozen shard with a base capture to take, else 1

    @property
    def workspaces(self) -> tuple[str, ...]:
        """The workspaces the shard loads, in first-use order (11.3: "loads only the workspaces it needs")."""
        return tuple(dict.fromkeys(cp.workspace_id for it in self.items for cp in it.checkpoints))


class _Rig:
    """Running aggregates of one rig, so its load is read in constant time."""

    def __init__(self, rig: RigLike):
        self.id, self.fp = rig.id, rig.fingerprint_id
        self.items: list[Item] = []
        self.frozen_s = self.masked_s = 0.0
        self.frozen_n = self.masked_n = self.base_n = 0
        self.ws: dict[str, Counter] = {"frozen": Counter(), "masked": Counter()}


_Cost = tuple[str, float, bool]               # (role, seconds on one rig, needs a base session)


@dataclass
class ShardLayout:
    """The packed plan: items per rig in placement order, the shards they form and each rig's load."""
    mode: str
    startups: Startups
    passes: int
    rig_order: tuple[str, ...]
    pairs: tuple[tuple[str, str], ...]
    unplaced: list = field(default_factory=list)        # masked_live items with no lockstep pair (3.1)
    _rigs: dict[str, _Rig] = field(default_factory=dict, repr=False)
    _cost: Callable[[Item, str], _Cost] | None = field(default=None, repr=False)

    def items_on(self, rig: str) -> list:
        """The items placed on a rig (both rigs of a pair carry its masked_live items)."""
        return list(self._rigs[rig].items)

    def cost(self, rig: str) -> RigCost:
        """The rig's load (11.2): captures, session startups and workspace switches."""
        r = self._rigs[rig]
        frozen_sessions = 0 if not r.frozen_n else (2 if r.base_n else 1)
        masked_sessions = 1 if r.masked_n else 0
        switches = (frozen_sessions * max(0, len(r.ws["frozen"]) - 1)
                    + masked_sessions * max(0, len(r.ws["masked"]) - 1))
        sessions = frozen_sessions + masked_sessions
        return RigCost(r.frozen_s + r.masked_s, sessions, sessions * self.startups.per_session_s,
                       switches * self.startups.workspace_switch_s)

    def load_s(self, rig: str) -> float:
        return self.cost(rig).total_s

    def sessions(self, rig: str) -> list[bool]:
        """One entry per session the rig opens, True when it includes a fixture reset (`scope.capture_time`)."""
        return [self.startups.fixture_reset] * self.cost(rig).sessions

    def shards(self) -> list[Shard]:
        """The shards of the layout, in pool order (choice 1): a masked_live shard per lockstep pair, a frozen shard
        per rig; in `aa` mode one shard per rig (data mode `any`: one session runs both modes, 10.6)."""
        out = [Shard(pair, "masked_live", items, 1) for pair in self.pairs
               if (items := [it for it in self._rigs[pair[0]].items if it.rigs == pair])]
        for rid in self.rig_order:
            r = self._rigs[rid]
            mine = [it for it in r.items if len(it.rigs) == 1]
            if mine:
                out.append(Shard((rid,), "any" if self.mode == "aa" else "frozen", mine, 2 if r.base_n else 1))
        return out

    def _item_cost(self, it: Item, fp: str) -> _Cost:
        assert self._cost is not None
        return self._cost(it, fp)

    def _add(self, it: Item, rid: str) -> None:
        r = self._rigs[rid]
        role, secs, needs_base = self._item_cost(it, r.fp)
        lane = "masked" if role == "masked" else "frozen"
        r.items.append(it)
        if lane == "masked":
            r.masked_s += secs
            r.masked_n += 1
        else:
            r.frozen_s += secs
            r.frozen_n += 1
            r.base_n += needs_base
        r.ws[lane].update({cp.workspace_id for cp in it.checkpoints})

    def remove(self, it: Item) -> None:
        """Take an item out of the plan (a budget cut, 2.11) and recompute its rigs' loads."""
        for rid in it.rigs:
            r = self._rigs[rid]
            role, secs, needs_base = self._item_cost(it, r.fp)
            lane = "masked" if role == "masked" else "frozen"
            r.items = [x for x in r.items if x is not it]
            if lane == "masked":
                r.masked_s -= secs
                r.masked_n -= 1
            else:
                r.frozen_s -= secs
                r.frozen_n -= 1
                r.base_n -= needs_base
            r.ws[lane].subtract({cp.workspace_id for cp in it.checkpoints})
            r.ws[lane] = +r.ws[lane]


def plan(items: Iterable[Item], rigs: Sequence[RigLike], p50: Mapping[str, float] | None = None,
         startups: Startups = Startups(), *, mode: str = "pair", passes: int = 1, reuse: Reuse | None = None,
         seed_s: Mapping[str, float] | None = None) -> ShardLayout:
    """Pack `items` onto `rigs` (module docstring): groups by `shard_key` (and data mode), longest first.

    `p50` gives per-side seconds by item id (else the item's `estimated_s`); `reuse(checkpoint_id, fingerprint)` says
    whether a frozen checkpoint's stored base is reusable on a rig of that fingerprint (None: never); `seed_s` is load
    a rig already carries (rig 5's Nightly canaries). Each placed item's `rigs` is set; a masked_live item with no
    pair in `pair` mode is left in `ShardLayout.unplaced` with its `rigs` empty."""
    if mode not in MODES:
        raise ValueError(f"unknown shard mode {mode!r}")
    if passes < 1:
        raise ValueError("passes must be at least 1")
    durations = p50 or {}
    pairs = tuple(rig_pairs(rigs)) if mode == "pair" else ()
    layout = ShardLayout(mode, startups, passes, tuple(r.id for r in rigs), pairs)
    layout._rigs = {r.id: _Rig(r) for r in rigs}
    seed = dict(seed_s or {})

    def per_side(it: Item) -> float:
        return float(durations.get(it.item_id, it.estimated_s))

    cache: dict[tuple[int, str], _Cost] = {}

    def item_cost(it: Item, fp: str) -> _Cost:
        """(role, seconds on one rig, needs a base session) of an item on a rig of fingerprint `fp`."""
        k = (id(it), fp)
        if k not in cache:
            s = per_side(it)
            if mode == "aa":
                cache[k] = ("single", passes * s, False)
            elif it.data_mode == "masked_live":
                cache[k] = ("masked", s, False)
            else:
                reused = reuse is not None and all(reuse(cp.checkpoint_id, fp) for cp in it.checkpoints
                                                   if cp.data_mode == "frozen")
                cache[k] = ("frozen", s if reused else 2 * s, not reused)
        return cache[k]

    layout._cost = item_cost
    groups: dict[tuple[str, str], list[Item]] = {}
    for it in items:
        lane = "masked" if mode == "pair" and it.data_mode == "masked_live" else "single"
        groups.setdefault((lane, it.shard_key), []).append(it)

    def load(rid: str) -> float:
        return layout.load_s(rid) + seed.get(rid, 0.0)

    def standalone(g: list[Item]) -> float:
        return sum(per_side(it) for it in g) * (passes if mode == "aa" else 1)

    order = sorted(groups.items(), key=lambda kv: (-standalone(kv[1]), stable_hash("|".join(kv[0]))))
    for (lane, _key), g in order:                   # masked_live groups first, onto pairs (11.3)
        if lane != "masked":
            continue
        if not pairs:
            layout.unplaced.extend(g)
            continue
        chosen = min(pairs, key=lambda pr: (max(load(pr[0]), load(pr[1])), pairs.index(pr)))
        for it in g:
            it.rigs = chosen
            for rid in chosen:
                layout._add(it, rid)
    for (lane, _key), g in order:                   # then frozen (or every A/A) group, onto the least loaded rig
        if lane == "masked":
            continue
        if not layout.rig_order:
            raise ValueError("no rig to place work on")
        rid = min(layout.rig_order, key=lambda r: (load(r), layout.rig_order.index(r)))
        for it in g:
            it.rigs = (rid,)
            layout._add(it, rid)
    for it in layout.unplaced:
        it.rigs = ()
    return layout
