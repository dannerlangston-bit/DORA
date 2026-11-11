"""Scope computation per run type: the Dependency Map, the static impact step and the plan.

Spec: 2.10 (Dependency Map, static impact step), 2.11 (scope per run type, common filters,
order-and-cut, smoke subset, the no-manifest cases, budget B), 2.5.2 and 2.5.3 (ladders and which surfaces
get which ladder), 3.1 (frozen base reuse, masked_live lockstep), 4.3 (change manifest), 4.4
(dependency map), 1.8 and 9.4 (`missed_in_pr_run`), 10.5 (quarantine records), 10.6 and 11.2 (the Nightly A/A
pass, Calibration runs, session startups), 11.3 (sharding).

`compute_scope` returns a `ScopePlan`: the planned checkpoints in run order and per rig, the shards (`shards.plan`),
each rig's budget and load, the cut list (`not_run` with reason `entitlement`, `quarantine`, `budget` or `scope`),
the run-level findings (`dependency_map_gap`, `manifest_missing`), the run-health lines the scope owes
(`ScopePlan.health_lines`) and `manifest_state`, the flag the classifier reads as input X of 3.11
(`ScopePlan.x_input`). `capture_time` is budget B (SD-03). `plan_nightly` and `plan_calibration` plan the two run
types that compute no scope (2.11). `load_ladders`, `measured_sizes`, `work_area` and `quarantine_ids` read the
config files these take (`registry/ladders.yaml`, `corpus/measured_sizes.yaml`, `configs/allowed.yaml`,
`quarantine.yaml`).

Choices where the spec is ambiguous (each is reported to the coordinator):

1. PR sweeps and flows follow the change scope (2.11 `change_surfaces`: declared, map-expanded and
   static surfaces); the smoke subset adds census checkpoints only (the 11.2 PR budget). In the
   no-manifest, non-UI case (b) the plan adds the tier-1 flows of class `standard` whose surfaces
   meet the smoke subset, so a backend change that moves values is still checked. No longer a
   choice since the spec states it; the number stays for the references to it.
2. PR census workspaces (2.11, 1.11). The pseudocode visits the canonical workspace only; 1.11
   visits the declared chart cell in both its tier A workspaces. Declared surfaces are visited in
   every workspace of `census.workspaces`; every other surface in the canonical one only.
3. Global surfaces (2.4 "tabs, sub-tabs, Setup entries, settings pages, and dialogs"): kinds `tab`,
   `subtab`, `menu`, `settings_page` and `dialog`. Hosting is `gadgets_present` of the workspace or
   the workspace being listed in the surface's `census.workspaces`.
4. Census dimensions (2.2.2, 2.4, 4.5): tier-1 surfaces repeat the cross product of
   `census.dimensions` in tier A workspaces, one checkpoint per combination; elsewhere the plain id.
5. Sweep and split checkpoints run in the tier C harness workspace and count toward the surface's primary persona,
   `personas[0]` (SD-17), whatever the workspace's tag.
6. Sharding (2.11, 11.3, SD-14; `shards.plan`): census items take the key (surface tab, data mode), the tab being
   the area segment of the first `kind: tab` surface up the `parent` chain, else of the top ancestor (a dialog
   joins its tab); a flow takes its own key (its tier and id) unless it shares a `state_scope` or a fixture with
   another flow, when the whole connected group takes one key; a sweep ladder keys on (surface, host) and a split
   ladder on (surface, splitter), so a ladder runs in its mandatory order in one session (2.5.1, 2.5.2). Groups
   are packed longest first onto the pool: `masked_live` groups onto lockstep pairs, with no pair `not_run
   (reason: budget)` (3.1), then frozen groups onto whichever rig ends least loaded. A flow with any `masked_live`
   checkpoint is a lockstep item; pins apply within a data mode, since a shard is all frozen or all masked_live.
   The soak rig and the Explorer rig are outside the pool (1.9, 11.3).
7. Cost on a rig (11.2): per-side duration from the run store's 30-day p50 (defaults 11.1: census visit 12 s,
   sweep or split step 8 s, flow 90, 75 or 60 s by tier, soak cycle 15 min) times 2 for a frozen item without a
   reusable base (base then candidate on one rig) and times 1 with one, reuse decided against the fingerprint of
   the rig the item is placed on; a `masked_live` item costs its per-side duration on each rig of its pair. Session
   startups (4 min, 8 min with a fixture reset) and workspace switches (45 s) come from `shards.Startups`. Budget B
   per rig is `capture_time(run_type, the rig's sessions)` (SD-03), less the Explorer's 90 minutes in RC; the cut
   compares it with the rig's captures plus switches. `budget_s` replaces B on every rig; `ScopePlan.budget_s` is B
   before startups.
8. Ladders (2.5.2, 2.5.3, 4.5). Ladder F's ascending pass revisits six grid sizes; its steps take the ids
   `sweep/<surface>/<host>/<w>x<h>-asc` (SD-04) and are planned after the descending pass, corners and corpus
   sizes, in ladder order. Every census, sweep and split id comes from `sentinel.checkpoints`, the one enumeration
   the lint shares. The tier and run type decide whether a surface sweeps and which splitter set it takes (RC: full
   for tiers 1 and 2, short for tier 3, SD-04); a `size_ladder` override replaces only the w x h steps, with the
   named ladder's steps in its order (its `run_types` are not read: the override applies wherever 2.5.3 sweeps the
   surface). A `streaming: true` surface, and a `kind: chart` surface when no frozen data exists (DR-02 branch B
   without OnDemand, `frozen_available=False`), plan no sweep or split item; those checkpoints are `not_run
   (reason: scope)` and the chart count prints in run health. Chart sweeps otherwise run `frozen`. A Ladder F
   surface without measured corpus sizes plans none and prints in run health (SD-06): there is no id to cut.
9. Static impact (2.10.2): depth 0 is the changed class itself. A surface reached only through its
   own roots is not a map gap (no primitive is involved); a changed class owned by another surface
   but no primitive is named as the unowned root of the missing edge. A changed class in no
   primitive's and no surface's roots is an `unowned_root` gap only when the reverse closure from
   it (same depth bound) reaches a surface's roots (2.11); a backend class reaching no surface is
   not a gap. Without a dependency index the unowned-root check is skipped with the rest of the
   static step, so exactly one gap appears.
10. `missed_in_pr_run` (1.8, 9.4): "had in scope" means the PR's change-derived scope (a smoke-only
    surface does not attribute a merge finding to every PR); "not_run (reason: scope)" means outside
    its census scope. Missable finding kinds are those of the Unexpected list: `collateral`,
    `invariant_violation`, `assertion_failed`, `needs_human`, `landmark_missing`.
11. Finding ids are `fnd-<run_id>-scope-<nnn>` and `fnd-<run_id>-merge-<nnn>`; gap details go in
    `evidence.raw_values` (4.13 carries no reason on a finding).
12. Attribution of a surface the static step reaches through its own roots (no primitive owns the changed
    class): 4.13 requires a `collateral` record to name a primitive, so the attribution names the surface's
    hosting primitive, the first non-global primitive whose Dependency Map edge reaches the surface (the Level II
    cell's FlexibleGrid); none when the map routes no primitive to it.
13. Proposed edges (2.10.2, 4.4): only with the candidate build id, since an added edge must name the build whose
    scan saw it; `depscan.map_update` stamps `first_seen` and `last_verified` when the map PR is written.
14. The Nightly A/A scope (10.6, 11.2): the checkpoint universe is the main build's merge pass (tier A census, Ladder
    S sweeps of tiers 1 and 2) plus every flow of the library; an item with any tier-1 checkpoint runs every night,
    every other item in the night `day.toordinal() % 5` that a stable hash of its key picks (a ladder rotates whole,
    as its steps run in order), so five consecutive nights cover everything. Both passes run in one session on one
    rig (`shards` mode `aa`); the rig-5 list (mutants 10, fire drill 15, safety canaries 20, fingerprint 5 min)
    loads the canary rig first. Entitlement and quarantine filters apply.
15. Calibration (3.9, 11.2): the weekend run places each item once (by its five passes) and runs the sixth pass on
    the same rig Saturday; both nights start 22:15 local, the start deadline is Saturday 02:00 and all passes end by
    Sunday 17:00 (all_quiet). The emergency run takes the items with a tier-1 checkpoint, the first three rigs and
    five passes from 22:15, ending by 06:00 with start deadline 01:00 (no sixth pass is named for it); every other
    checkpoint is listed as left `needs_human (reason: uncalibrated)`.
"""
from __future__ import annotations

import fnmatch
import itertools
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Iterable, Iterator, Mapping, Sequence

from . import checkpoints as cps
from .records import DATA_MODES, PERSONAS, CheckpointOutcome, Finding
from .shards import Shard, ShardLayout, Startups, stable_hash
from .shards import plan as shard_plan
from .taxonomy import is_checkpoint_id

SMOKE_CAP = 40                                   # 2.2.4, L-SRF-15
STATIC_MAX_DEPTH = 6                             # 2.10.2
EXPLORER_BUDGET_S = 90 * 60                      # 2.9
SCOPED_RUN_TYPES = ("pr", "merge", "rc")         # 2.11: Nightly and Calibration take their own lists
DEFAULT_DURATION_S = {"census": 12.0, "sweep": 8.0, "split": 8.0, "flow": 90.0}               # 11.1 PA-03, PA-07
FLOW_DEFAULT_S = {1: 90.0, 2: 75.0, 3: 60.0}     # 11.1 PA-06: seconds per flow by tier
SOAK_CYCLES = 24                                 # 1.9, 10.11
SOAK_CYCLE_S = 6 * 3600 / SOAK_CYCLES
LADDER_S = tuple((w, h) for w in (960, 640, 480, 320) for h in (720, 480, 360))               # 2.5.2
LADDER_F_GRID = tuple((w, h) for w in (960, 800, 640, 480, 400, 320) for h in (720, 600, 480, 420, 360, 300))
LADDER_F_ASCENDING = tuple((w, 480) for w in (320, 400, 480, 640, 800, 960))                  # 2.5.2, SD-04
GLOBAL_KINDS = cps.GLOBAL_KINDS
KIND_ORDER = {"soak": 0, "flow": 0, "sweep": 1, "split": 2, "census": 3}
MISSABLE_KINDS = frozenset({"collateral", "invariant_violation", "assertion_failed", "needs_human", "landmark_missing"})


class ScopeConfigError(ValueError):
    """An input lint should have refused (2.2.4, 4.15); the run ends aborted_harness (config_invalid) with the rule
    ids the message names (`rule_ids`, e.g. L-SRF-15 for the smoke cap, SD-01), none when the message names none."""

    @property
    def rule_ids(self) -> tuple[str, ...]:
        """The lint rule ids this error names, in order of appearance (the abort block's `rule_ids`, 4.15)."""
        return tuple(dict.fromkeys(_RULE_ID.findall(str(self))))


_RULE_ID = re.compile(r"\bL-[A-Z]+-\d+\b")


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


def capture_time(run_type: str, sessions: int | Iterable[bool] = 0, startups: Startups = Startups()) -> float:
    """Budget B of 2.11 for one rig (SD-03): the run type's wall budget (11.2) minus the 10-minute report margin minus
    the startup of every session the rig runs (4 min, 8 min for a session that includes a fixture reset; PA-04).

    `sessions` is a count of plain sessions or one flag per session, True when it includes a fixture reset. An RC
    plan subtracts the Explorer's 90 minutes on top (2.11 `scope_rc`); `compute_scope` does that."""
    from .orchestrator.budgets import REPORT_MARGIN_S, WALL_BUDGET_S       # the one table of 11.2 wall budgets
    if run_type not in WALL_BUDGET_S:
        raise ValueError(f"run type {run_type!r} has no wall budget (11.2)")
    flags = [False] * sessions if isinstance(sessions, int) else [bool(f) for f in sessions]
    startup = sum(startups.fixture_reset_s if f else startups.session_s for f in flags)
    return WALL_BUDGET_S[run_type] - REPORT_MARGIN_S - startup


# -- inputs: registry, corpus, flows (4.5, 4.6, 4.7) ------------------------------------------

@dataclass(frozen=True)
class Splitter:
    """An in-host splitter of a registry entry (2.2.1, 4.5)."""
    id: str
    host: str
    fractions: tuple[float, ...] | None = None
    collapsible: bool = False

    def steps(self, full: bool) -> tuple[float, ...]:
        """The short or full fraction set of 2.5.2 narrowed by `fractions`; 0.00 and 1.00 only when collapsible
        (`checkpoints.split_fractions`)."""
        return cps.split_fractions(self.fractions, self.collapsible, full=full)


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
    parent: str | None = None
    streaming: bool = False
    size_ladder: str | None = None
    ladder_reason: str | None = None

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
            parent=d.get("parent"), streaming=bool(d.get("streaming", False)), size_ladder=d.get("size_ladder"),
            ladder_reason=d.get("ladder_reason"),
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

    def tab(self, sid: str) -> str:
        """The top-level tab a surface belongs to, with its dialogs and menus (11.3 census shard key): the area
        segment of the first `kind: tab` surface up its `parent` chain, else of its top ancestor (choice 6)."""
        seen: set[str] = set()
        cur = sid
        while cur in self.surfaces and cur not in seen:
            seen.add(cur)
            s = self.surfaces[cur]
            if s.kind == "tab" or not s.parent:
                break
            cur = s.parent
        return cur.split(".")[0]

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
    """A `checkpoint` step of a flow (4.7); `tier` is the step's override of the surface tier."""
    checkpoint_id: str
    surface_id: str
    step_id: str
    data_mode: str
    tier: int | None = None


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
    state_scope: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping) -> "Flow":
        default_mode = d.get("data_mode", "frozen")
        cps = tuple(FlowCheckpoint(s["checkpoint_id"], s["surface_id"], s["id"], s.get("data_mode", default_mode),
                                   int(s["tier"]) if s.get("tier") is not None else None)
                    for s in d.get("steps") or () if s.get("type") == "checkpoint")
        return cls(flow_id=d["flow_id"], tier=int(d["tier"]), persona=d["persona"], workspace=d["workspace"],
                   surfaces=tuple(d.get("surfaces") or ()), checkpoints=cps,
                   requires=frozenset(d.get("requires") or ()), fixture=d.get("fixture"),
                   data_mode=default_mode, flow_class=d.get("class", "standard"), state_scope=d.get("state_scope"))


@dataclass(frozen=True)
class LadderDef:
    """A named ladder of `registry/ladders.yaml` (2.5.2, 4.5 `size_ladder`): its steps as (w, h, part), where w may
    be `MAXW` and h `MAXH` and part is grid, corner or ascending."""
    id: str
    name: str
    steps: tuple[tuple[int | str, int | str, str], ...]
    corpus_sizes: bool = False

    @classmethod
    def from_dict(cls, d: Mapping) -> "LadderDef":
        return cls(str(d["id"]), str(d.get("name", "")),
                   tuple((st["w"], st["h"], str(st.get("part", "grid"))) for st in d.get("steps") or ()),
                   bool(d.get("corpus_sizes", False)))

    def sweep_steps(self, max_client: tuple[int, int], corpus: Sequence[cps.SweepStep] = ()) -> tuple[cps.SweepStep, ...]:
        """The ladder's steps in order, MAXW and MAXH resolved, the corpus sizes after the corners when the ladder
        includes them, the ascending part as `-asc` steps (2.5.2, SD-04)."""
        maxw, maxh = max_client
        out: list[cps.SweepStep] = []
        for w, h, part in self.steps:
            if part == "ascending" and self.corpus_sizes and corpus and not any(st.ascending for st in out):
                out += corpus
            out.append(cps.SweepStep(maxw if w == "MAXW" else int(w), maxh if h == "MAXH" else int(h), part == "ascending"))
        if self.corpus_sizes and corpus and not any(st.ascending for st in out):
            out += corpus
        return tuple(dict.fromkeys(out))


def load_ladders(doc: Mapping | None) -> dict[str, LadderDef]:
    """The ladders of `registry/ladders.yaml` by id (2.5.2); an absent file gives none."""
    return {lad.id: lad for lad in (LadderDef.from_dict(x) for x in ((doc or {}).get("ladders") or ()))}


def measured_sizes(doc: Mapping | None) -> dict[str, tuple[cps.SweepStep, ...]]:
    """Ladder F's corpus sizes by surface from `corpus/measured_sizes.yaml` (2.5.2, 4.16, SD-06), each on the host it
    was measured on; an absent file gives none, so those steps are `not_run (reason: scope)` (`ScopePlan`)."""
    out: dict[str, list[cps.SweepStep]] = {}
    for x in (doc or {}).get("sizes") or ():
        out.setdefault(str(x["surface"]), []).append(cps.SweepStep(int(x["w"]), int(x["h"]), host=str(x["host"])))
    return {k: tuple(dict.fromkeys(v)) for k, v in out.items()}


def work_area(doc: Mapping | None, config_id: str | None = None) -> tuple[int, int] | None:
    """MAXW and MAXH: the `work_area` of the pinned display configuration in `configs/allowed.yaml` (2.5.2, 4.6,
    SD-06), the one named `config_id` or else the first entry; None when no entry carries one."""
    for c in (doc or {}).get("configs") or ():
        if config_id in (None, c.get("id")) and isinstance(c.get("work_area"), Mapping):
            return int(c["work_area"]["w"]), int(c["work_area"]["h"])
    return None


@dataclass(frozen=True)
class QuarantineRecord:
    """A record of `quarantine.yaml` (10.5, 4.16, SD-96): exactly one of a checkpoint or a flow, and a reason."""
    checkpoint: str | None
    flow: str | None
    reason: str
    owner: str = ""
    ticket: str = ""

    @property
    def target(self) -> str:
        """The quarantined id: the checkpoint id or the flow id."""
        return self.checkpoint or self.flow or ""


def quarantine_records(doc: Mapping | None) -> tuple[QuarantineRecord, ...]:
    """The records of a linted `quarantine.yaml` (L-XREF-11 checks that each names exactly one existing checkpoint
    or flow)."""
    return tuple(QuarantineRecord(r.get("checkpoint"), r.get("flow"), str(r.get("reason", "")), str(r.get("owner", "")),
                                  str(r.get("ticket", "")))
                 for r in (doc or {}).get("records") or () if isinstance(r, Mapping))


def quarantine_ids(doc: Mapping | None) -> frozenset[str]:
    """Q of 2.11: the checkpoint ids and flow ids `quarantine.yaml` names, the `quarantine` input of
    `compute_scope`; a quarantined flow takes every checkpoint of the flow out (10.5)."""
    return frozenset(r.target for r in quarantine_records(doc) if r.target)


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
    """A 4.4 `ts` as ISO 8601 text (YAML reads an unquoted timestamp as a datetime)."""
    if v is None:
        return None
    return v.isoformat() if isinstance(v, (datetime, date)) else str(v)


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
    reaching = {origin for _, origin in best}             # 2.11: only a class that reaches a surface is a gap
    for c in sorted(changed):
        if not owners[c] and not registry.owning_surfaces(c) and c in reaching:
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
    declared: bool = False                       # on a declared surface: first within its tier (2.11, SD-14)

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
    rig_budget_s: dict[str, float] = field(default_factory=dict)     # B per rig: capture time after its startups
    rig_load_s: dict[str, float] = field(default_factory=dict)       # per rig: captures, switches and startups
    shards: list[Shard] = field(default_factory=list)
    chart_sweeps_not_run: int = 0          # 2.5.3: chart sweep checkpoints not_run (scope) under DR-02 branch B
    corpus_sizes_missing: tuple[str, ...] = ()   # 2.5.2: Ladder F surfaces without measured corpus sizes

    def health_lines(self) -> list[str]:
        """The run-health lines the scope owes (2.5.2, 2.5.3, 2.11): chart sweeps cut under DR-02 branch B, Ladder F
        corpus sizes not yet measured, and the projected overrun per rig."""
        out = []
        if self.chart_sweeps_not_run:
            out.append(f"chart sweeps not run (scope): {self.chart_sweeps_not_run} checkpoints; DR-02 branch B without "
                       "OnDemand has no frozen chart data (2.5.3)")
        if self.corpus_sizes_missing:
            out.append("Ladder F corpus sizes not run (scope): corpus/measured_sizes.yaml lists no size for "
                       + ", ".join(self.corpus_sizes_missing) + " (2.5.2)")
        for rig, over in sorted(self.projected_overrun_s.items()):
            out.append(f"projected overrun on {rig}: {over / 60:.0f} min over its capture budget (2.11)")
        return out

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
    declared: frozenset[str]
    durations: Mapping[str, float]
    base_reuse: BaseReuse | None
    max_client: tuple[int, int]
    corpus_sizes: Mapping[str, Sequence[tuple[int, int] | cps.SweepStep]]
    ladders: Mapping[str, LadderDef] = field(default_factory=dict)
    frozen_available: bool = True
    seq: Iterator[int] = field(default_factory=itertools.count)
    excluded: list[Cut] = field(default_factory=list)       # 2.5.3: streaming surfaces and branch-B chart sweeps
    chart_cut: int = 0
    corpus_missing: set[str] = field(default_factory=set)

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
                   persona: str | None = None, base: bool = True, tier: int | None = None, **kw) -> PlannedCheckpoint:
        """A planned checkpoint; `base=False` for candidate-only work (the soak, 1.9); `tier` overrides the surface's
        (a flow checkpoint step's `tier`, 4.7)."""
        if not is_checkpoint_id(cid):
            raise ScopeConfigError(f"checkpoint id {cid!r} is outside the grammar of 4.1")
        if data_mode not in DATA_MODES:
            raise ScopeConfigError(f"data_mode {data_mode!r} on {cid}")
        reuse = self.base_reuse.source_for(cid) if base and self.base_reuse and data_mode == "frozen" else None
        return PlannedCheckpoint(cid, s.id, kind, s.tier if tier is None else tier, w.id, persona or self.persona(w, s),
                                 data_mode, base_source_run_id=reuse, **kw)

    def item(self, item_id: str, kind: str, tier: int, cps: list[PlannedCheckpoint], data_mode: str,
             shard_key: str, protected: bool, estimated_s: float, declared: bool = False) -> PlanItem:
        return PlanItem(item_id, kind, tier, cps, data_mode, shard_key, protected, estimated_s, next(self.seq),
                        declared=declared)

    def duration(self, key: str, kind: str, tier: int = 1) -> float:
        """Per-side seconds: the 30-day store p50 by id, else the 11.1 planning default (flows by tier, PA-06)."""
        default = FLOW_DEFAULT_S.get(tier, DEFAULT_DURATION_S["flow"]) if kind == "flow" else DEFAULT_DURATION_S[kind]
        return float(self.durations.get(key, default))


def _census_items(ctx: _Ctx, s: Surface, workspace_ids: Iterable[str]) -> list[PlanItem]:
    out = []
    key = f"census:{ctx.registry.tab(s.id)}:{s.data_mode}"          # 11.3: (surface tab, data mode), SD-14
    for wid in workspace_ids:
        w = ctx.workspace(wid)
        base = f"census/{s.id}/{w.id}"
        ids = cps.census_ids(s.id, w.id, surface_tier=s.tier, workspace_tier=w.tier,
                             dimensions=dict(s.census_dimensions))
        planned = [ctx.checkpoint(cid, s, "census", w, s.data_mode) for cid in ids]
        est = sum(ctx.duration(cid, "census") for cid in ids)
        out.append(ctx.item(base, "census", s.tier, planned, s.data_mode, key, s.id in ctx.protected_surfaces, est,
                            s.id in ctx.declared))
    return out


def _ladder(s: Surface, ctx: _Ctx) -> tuple[tuple[cps.SweepStep, ...], bool | None, bool]:
    """2.5.3: (steps, full splitter set?, corpus sizes missing?) for a resizable surface in this run type; no sweep
    -> ((), None, False). The tier and run type decide whether the surface sweeps and which splitter set it takes; a
    `size_ladder` override replaces the w x h ladder (choice 8). Ladder F ends with its ascending pass, whose steps
    take the `-asc` ids (SD-04); RC sweeps tier 3 on Ladder S with the short splitter set (SD-04)."""
    def steps(sizes: Iterable[tuple[int, int]], ascending: bool = False) -> tuple[cps.SweepStep, ...]:
        return tuple(cps.SweepStep(w, h, ascending) for w, h in dict.fromkeys(sizes))
    if ctx.run_type == "pr":
        swept, full, ladder_f = s.tier == 1, False, False
    elif ctx.run_type == "merge":
        swept, full, ladder_f = s.tier <= 2, False, False
    else:
        swept, full, ladder_f = True, s.tier <= 2, s.tier <= 2
    if not swept:
        return (), None, False
    corpus = tuple(dict.fromkeys(c if isinstance(c, cps.SweepStep) else cps.SweepStep(int(c[0]), int(c[1]))
                                 for c in ctx.corpus_sizes.get(s.id, ())))
    if s.size_ladder is not None:
        lad = ctx.ladders.get(s.size_ladder)
        if lad is None:
            raise ScopeConfigError(f"{s.id}: size_ladder {s.size_ladder} is not in registry/ladders.yaml (L-SRF-03)")
        return lad.sweep_steps(ctx.max_client, corpus), full, lad.corpus_sizes and not corpus
    if not ladder_f:
        return steps(LADDER_S), full, False
    maxw, maxh = ctx.max_client
    corners = ((240, 160), (240, maxh), (maxw, 160), (maxw, maxh))
    descending = steps(LADDER_F_GRID + corners) + corpus     # a measured corpus size keeps its host (SD-06)
    return descending + steps(LADDER_F_ASCENDING, ascending=True), full, not corpus


def _sweep_items(ctx: _Ctx, s: Surface) -> list[PlanItem]:
    """The sweep and split items of a resizable surface (2.5.2, 2.5.3, 2.11 `sweeps`). A `streaming: true` surface
    and, without frozen data (DR-02 branch B without OnDemand), a chart surface contribute none: their sweep
    checkpoints go to `ctx.excluded` as `not_run (reason: scope)`. Chart sweeps run frozen only."""
    if not s.resizable:
        return []
    sizes, full, corpus_missing = _ladder(s, ctx)
    if not sizes and full is None:
        return []
    w = ctx.harness()
    chart = s.kind == "chart"
    mode = "frozen" if chart else s.data_mode
    persona = s.personas[0] if s.personas else None         # SD-17: sweeps count toward the primary persona
    protected = s.id in ctx.protected_surfaces
    declared = s.id in ctx.declared
    out = []
    seen: set[str] = set()
    for host in cps.sweep_hosts(s.hosts):    # SD-05: tab and sidebar hosts are swept through their splitters only
        for st in sizes:
            if st.host not in (None, host):  # a corpus size is a step of the host it was measured on only
                continue
            wd, ht = st.w, st.h
            cid = cps.sweep_id(s.id, host, wd, ht, ascending=st.ascending)
            if cid in seen:                  # a corpus size equal to a grid size is that step, visited once
                continue
            seen.add(cid)
            cp = ctx.checkpoint(cid, s, "sweep", w, mode, persona=persona,
                                size_step=(("host", host), ("w", wd), ("h", ht)))
            out.append(ctx.item(cid, "sweep", s.tier, [cp], mode, f"sweep:{s.id}:{host}", protected,
                                ctx.duration(cid, "sweep"), declared))
    if full is not None:
        for sp in s.splitters:
            for frac in sp.steps(full):
                cid = cps.split_id(s.id, sp.id, frac)
                cp = ctx.checkpoint(cid, s, "split", w, mode, persona=persona,
                                    size_step=(("splitter", sp.id), ("fraction", frac)))
                out.append(ctx.item(cid, "split", s.tier, [cp], mode, f"split:{s.id}:{sp.id}", protected,
                                    ctx.duration(cid, "split"), declared))
    if s.streaming or (chart and not ctx.frozen_available):
        ctx.excluded.extend(Cut("scope", cp.surface_id, cp.tier, cp) for it in out for cp in it.checkpoints)
        if not s.streaming:
            ctx.chart_cut += sum(len(it.checkpoints) for it in out)
        return []
    if corpus_missing:
        ctx.corpus_missing.add(s.id)
    return out


def _flow_item(ctx: _Ctx, f: Flow) -> PlanItem:
    w = ctx.workspace(f.workspace)
    cps = []
    for fc in f.checkpoints:
        s = ctx.registry[fc.surface_id] if fc.surface_id in ctx.registry else Surface(fc.surface_id, f.tier)
        cps.append(ctx.checkpoint(fc.checkpoint_id, s, "flow", w, fc.data_mode, persona=f.persona, tier=fc.tier,
                                  flow_id=f.flow_id, step_id=fc.step_id))
    mode = "masked_live" if any(c.data_mode == "masked_live" for c in f.checkpoints) else "frozen"
    protected = bool(set(f.surfaces) & ctx.protected_surfaces)
    return ctx.item(f.flow_id, "flow", f.tier, cps, mode, f"flow:{f.tier}:{f.flow_id}", protected,
                    ctx.duration(f.flow_id, "flow", f.tier), bool(set(f.surfaces) & ctx.declared))


def _pin_flows(items: list[PlanItem], flows: Mapping[str, Flow]) -> None:
    """2.11, 11.3 (SD-14): flows sharing a `state_scope` or a fixture take one shard key, so they land in one shard;
    every other flow keeps its own key, a stable hash of its id within its tier (choice 6)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    flow_items = [it for it in items if it.kind == "flow"]
    for it in flow_items:
        f = flows[it.item_id]
        for pin in (f"state_scope:{f.state_scope}" if f.state_scope else None, f"fixture:{f.fixture}" if f.fixture else None):
            if pin:
                a, b = find(it.item_id), find(pin)
                if a != b:
                    parent[max(a, b)] = min(a, b)
    members: dict[str, list[PlanItem]] = {}
    for it in flow_items:
        members.setdefault(find(it.item_id), []).append(it)
    for group in members.values():
        if len(group) > 1:
            root = min(it.item_id for it in group)
            for it in group:
                it.shard_key = f"flow:pin:{root}"


UNSCHEDULED_CLASSES = ("fixture_reset", "login")


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


def _pass_items(ctx: _Ctx, run_type: str, flows: Iterable[Flow]) -> tuple[list[PlanItem], list[Cut]]:
    """The merge or RC census, sweep and flow items before the filters (2.11 `scope_merge`, `scope_rc`): every
    surface in every tier A (merge) or tier A and B (RC) workspace that hosts it or for a global surface, the
    sweeps of the run type's ladder, and the tier-1 flows (merge) or every flow (RC)."""
    items: list[PlanItem] = []
    cuts: list[Cut] = []
    pass_ws = sorted(w.id for w in ctx.workspaces.values() if w.tier in cps.PASS_TIERS[run_type])
    for s in ctx.registry:
        if s.census_enabled:
            for wid in pass_ws:
                w = ctx.workspaces[wid]
                if cps.pass_visits(s.id, s.kind, s.census_workspaces, wid, w.gadgets_present):
                    items += _census_items(ctx, s, [wid])
                else:
                    cp = ctx.checkpoint(f"census/{s.id}/{wid}", s, "census", w, s.data_mode)
                    cuts.append(Cut("scope", s.id, s.tier, cp))
        items += _sweep_items(ctx, s)
    selected = [f for f in flows if run_type == "rc" or f.tier == 1]
    items += [_flow_item(ctx, f) for f in sorted(selected, key=lambda f: f.flow_id)]
    return items, cuts


def _common_filters(items: list[PlanItem], ctx: _Ctx, flows: Mapping[str, Flow], entitlements: frozenset[str],
                    quarantine: frozenset[str]) -> tuple[list[PlanItem], list[Cut]]:
    """`common_filters` of 2.11: entitlement first, then quarantine; a quarantined flow takes all its checkpoints
    out, a quarantined checkpoint only itself (10.5)."""
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
        if it.kind == "flow" and it.item_id in quarantine:
            cuts.extend(Cut("quarantine", cp.surface_id, cp.tier, cp) for cp in it.checkpoints)
            continue
        q = [cp for cp in it.checkpoints if cp.checkpoint_id in quarantine]
        cuts.extend(Cut("quarantine", cp.surface_id, cp.tier, cp) for cp in q)
        it.checkpoints = [cp for cp in it.checkpoints if cp.checkpoint_id not in quarantine]
        if it.checkpoints:
            kept.append(it)
    return kept, cuts


def _run_order(it: PlanItem) -> tuple:
    """2.11 `order_and_cut` sort (SD-14): tier ascending, declared surfaces first within a tier, kind (flows, sweeps,
    census), census grouped by workspace, estimated duration descending, then planning order."""
    ws = it.checkpoints[0].workspace_id if it.kind == "census" and it.checkpoints else ""
    return (it.tier, not it.declared, KIND_ORDER[it.kind], ws, -it.estimated_s, it.order)


@dataclass
class _Placed:
    """What `_order_and_cut` returns to `compute_scope`."""
    kept: list[PlanItem]
    by_rig: dict[str, list[PlanItem]]
    cuts: list[Cut]
    overrun: dict[str, float]
    budget: dict[str, float]
    load: dict[str, float]
    shards: list[Shard]


def _order_and_cut(items: list[PlanItem], rigs: Sequence[Rig], soak_rig: Rig | None, run_type: str,
                   budget_s: float | None, explorer_rig: str | None = None, base_reuse: BaseReuse | None = None,
                   startups: Startups = Startups()) -> _Placed:
    """`order_and_cut` of 2.11: sort, shard (`shards.plan`), then while a rig's capture time exceeds its budget B cut
    the last item of the highest tier on it, never a declared or smoke surface."""
    if not rigs:
        raise ScopeConfigError("a run needs at least one rig")
    # L-07, L-08 (1.9, 11.2, 11.3): the soak rig carries only the soak and the Explorer rig no
    # scripted work, so neither is in the sharding pool.
    pool = [r for r in rigs if r.id != explorer_rig and (soak_rig is None or r.id != soak_rig.id)]
    if not pool and any(it.kind != "soak" for it in items):
        raise ScopeConfigError("a run needs at least one rig for scripted work besides the soak and Explorer rigs")
    pool_ids = {r.id for r in pool}
    fingerprint = {r.id: r.fingerprint_id for r in pool}
    items = sorted(items, key=_run_order)
    soak = [it for it in items if it.kind == "soak"]
    if soak and soak_rig is None:
        raise ScopeConfigError("soak cycles need the soak rig (1.9)")
    reuse = (lambda cid, fp: base_reuse.source_for(cid, fp) is not None) if base_reuse is not None else None
    layout: ShardLayout = shard_plan([it for it in items if it.kind != "soak"], pool, None, startups, reuse=reuse)
    cuts = [Cut("budget", cp.surface_id, cp.tier, cp) for it in layout.unplaced for cp in it.checkpoints]
    unplaced = {id(it) for it in layout.unplaced}
    for it in soak:
        it.rigs = (soak_rig.id,) if soak_rig else ()
    placed = [it for it in items if id(it) not in unplaced]
    if base_reuse is not None:
        for it in placed:
            if it.kind != "soak":
                # L-09, 3.1: a frozen base is reused only under the fingerprint of the rig the item runs on.
                fp = fingerprint[it.rigs[0]]
                it.checkpoints = [replace(cp, base_source_run_id=base_reuse.source_for(cp.checkpoint_id, fp))
                                  if cp.data_mode == "frozen" else cp for cp in it.checkpoints]

    rig_ids = [r.id for r in rigs] + ([soak_rig.id] if soak_rig and soak_rig.id not in {r.id for r in rigs} else [])
    soak_s = sum(it.estimated_s for it in soak)
    explorer = EXPLORER_BUDGET_S if run_type == "rc" else 0.0

    def work(r: str) -> float:
        if r in pool_ids:
            return layout.cost(r).work_s
        return soak_s if soak and soak_rig and r == soak_rig.id else 0.0

    def sessions(r: str) -> list[bool]:
        if r in pool_ids:
            return layout.sessions(r)
        return [startups.fixture_reset] if soak and soak_rig and r == soak_rig.id else []

    def budget(r: str) -> float:
        return float(budget_s) if budget_s is not None else capture_time(run_type, sessions(r), startups) - explorer

    removed: set[int] = set()
    while True:
        victim = None
        for r in rig_ids:
            if work(r) <= budget(r):
                continue
            cand = [it for it in placed if r in it.rigs and not it.protected and id(it) not in removed]
            if cand:
                top = max(it.tier for it in cand)
                victim = [it for it in cand if it.tier == top][-1]
                break
        if victim is None:
            break
        removed.add(id(victim))
        layout.remove(victim)
        cuts.extend(Cut("budget", cp.surface_id, cp.tier, cp) for cp in victim.checkpoints)
    kept = [it for it in placed if id(it) not in removed]
    by_rig = {r: [it for it in kept if r in it.rigs] for r in rig_ids}
    overrun = {r: work(r) - budget(r) for r in rig_ids if work(r) > budget(r)}
    load = {r: (layout.load_s(r) if r in pool_ids else work(r) + len(sessions(r)) * startups.per_session_s)
            for r in rig_ids}
    return _Placed(kept, by_rig, cuts, overrun, {r: budget(r) for r in rig_ids}, load, layout.shards())


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
            if not prims:      # choice 12: reached through the surface's own roots; its hosting primitive on the map
                prims = sorted(p for p, d in dep_map.primitives.items()
                               if not d.is_global and sid in dep_map.surfaces_of(p, registry))
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
                  corpus_sizes: Mapping[str, Sequence[tuple[int, int] | cps.SweepStep]] | None = None,
                  soak: SoakPlan | None = None, explorer_rig: str | None = None,
                  map_scan_failed: bool = False, ladders: Mapping[str, LadderDef] | None = None,
                  frozen_available: bool = True, startups: Startups = Startups()) -> ScopePlan:
    """The scope of a PR, merge or RC run (2.11).

    `entitlements` is the profile E of the run's logins, `quarantine` the checkpoint and flow ids
    of `quarantine.yaml` (Q, `quarantine_ids`), `durations` the 30-day per-side p50 by checkpoint id (flows by flow
    id; the 11.1 defaults otherwise). The budget B of each rig is `capture_time(run_type, its sessions)`, less the
    Explorer's 90 minutes in RC (SD-03); `budget_s` replaces B on every rig. A PR manifest naming another candidate is
    treated as missing (L-MAN-09); merge and RC runs take the union manifest, `Manifest.union`, which is never absent
    (4.3). `soak` and `explorer_rig` apply to RC runs, and `map_scan_failed` to merge runs (2.10.1). The soak and
    Explorer rigs never take scripted work (11.3). `ladders` (`load_ladders`) resolves `size_ladder` overrides;
    `corpus_sizes` (`measured_sizes`) and `max_client` (`work_area`) feed Ladder F (SD-06); `frozen_available` is
    False under DR-02 branch B without OnDemand, which cuts chart sweeps (2.5.3); `startups` sets the 11.2 session
    costs.
    """
    if run_type in ("nightly", "calibration"):
        raise ValueError(f"{run_type} runs compute no scope; they take the canary and calibration lists (2.11): "
                         "plan_nightly and plan_calibration")
    if run_type not in SCOPED_RUN_TYPES:
        raise ValueError(f"unknown run type {run_type!r}")
    wmap = {w.id: w for w in workspaces}
    # the fixture reset and LOGIN flows are the Orchestrator's own, run at their gates (7.6, 7.7): never scheduled
    flow_map = {f.flow_id: f for f in flows if f.flow_class not in UNSCHEDULED_CLASSES}
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

    ctx = _Ctx(run_type, registry, wmap, declared | smoke, declared, durations or {}, base_reuse, max_client,
               corpus_sizes or {}, ladders or {}, frozen_available)
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
                    (set(f.surfaces) & scope_surfaces or _flow_primitives(f, registry) & touched
                     or (manifest_state == "no_manifest_nonui" and f.flow_class == "standard"
                         and set(f.surfaces) & smoke))]          # case (b): smoke flows still check moved values
        items += [_flow_item(ctx, f) for f in sorted(selected, key=lambda f: f.flow_id)]
    else:
        items, cuts = _pass_items(ctx, run_type, flow_map.values())
    if run_type == "rc" and soak is not None:
        items += _soak_items(ctx, soak)
    _pin_flows(items, flow_map)

    items, filtered = _common_filters(items, ctx, flow_map, E, Q)
    placed = _order_and_cut(items, rigs, soak.rig if soak and run_type == "rc" else None, run_type, budget_s,
                            explorer_rig, base_reuse, startups)
    explorer = EXPLORER_BUDGET_S if run_type == "rc" else 0.0
    budget = float(budget_s) if budget_s is not None else capture_time(run_type, 0, startups) - explorer

    proposed = []
    if impact is not None and candidate_build_id:       # 4.4: an added edge names the build whose scan saw it
        for g in impact.gaps:
            if g.primitive and g.surface:
                proposed.append(Edge(g.primitive, g.surface, "static_analysis", 1.0, True, candidate_build_id,
                                     g.evidence))
    return ScopePlan(run_id=run_id, run_type=run_type, budget_s=budget, manifest_state=manifest_state,
                     declared=declared, scope_surfaces=scope_surfaces, census_surfaces=census_surfaces,
                     items=placed.kept, by_rig=placed.by_rig, cuts=cuts + ctx.excluded + filtered + placed.cuts,
                     findings=fs.items, projected_overrun_s=placed.overrun,
                     attribution=_attribution(registry, dep_map, manifest, impact), proposed_edges=proposed,
                     impact=impact, explorer_rig=explorer_rig if run_type == "rc" else None,
                     rig_budget_s=placed.budget, rig_load_s=placed.load, shards=placed.shards,
                     chart_sweeps_not_run=ctx.chart_cut, corpus_sizes_missing=tuple(sorted(ctx.corpus_missing)))


# -- Nightly A/A pass and Calibration runs (10.6, 11.2, 3.9) -------------------------------------

NIGHTLY_ROTATION = 5                          # 10.6: a rotating 20 percent of the non-tier-1 checkpoints
NIGHTLY_PASSES = 2                            # 10.6: each checkpoint captured twice in one session
NIGHTLY_WINDOW_S = 4 * 3600                   # 11.2: the 4 h window, dead-man 06:00
NIGHTLY_RIG5_WORK_S = (("behavioural_mutants", 10 * 60.0), ("fire_drill", 15 * 60.0),
                       ("safety_canaries", 20 * 60.0), ("fingerprint_check", 5 * 60.0))     # 11.2 Nightly row
CALIBRATION_KINDS = ("weekend", "emergency")
CALIBRATION_START = time(22, 15)              # 11.2: the five passes start about 22:15
WEEKEND_PASSES = (5, 1)                       # 11.2, 3.9: five passes Friday night, a sixth Saturday night
EMERGENCY_PASSES = (5,)                       # 11.2 Emergency calibration: five passes, the same night
EMERGENCY_RIGS = 3                            # 11.2: pool rigs 1 to 3; rigs 4 and 5 carry the Nightly run
EMERGENCY_END = time(6, 0)                    # 11.2: the operating day starts at 06:00
EMERGENCY_START_DEADLINE = time(1, 0)         # 11.2
WEEKEND_START_DEADLINE = time(2, 0)           # 11.2: Saturday 02:00
ALL_QUIET_END = time(17, 0)                   # 7.10, 11.2: all_quiet ends Sunday 17:00


@dataclass
class NightlyPlan:
    """The Nightly run's capture work (10.6, 11.2): the A/A items sharded over the Nightly rigs, each captured twice in
    one session on one rig, and the rig-5 work list (behavioural mutants, fire drill, safety canaries, fingerprint
    check)."""
    day: date
    rotation_slice: int                       # the fifth of the non-tier-1 work this night carries
    items: list[PlanItem]
    by_rig: dict[str, list[PlanItem]]
    canary_rig: str
    rig5_work: tuple[tuple[str, float], ...]
    rig_load_s: dict[str, float]
    shards: list[Shard]
    cuts: list[Cut]
    window_s: float = NIGHTLY_WINDOW_S

    @property
    def aa_checkpoints(self) -> list[PlannedCheckpoint]:
        """The checkpoints of the A/A pass, in run order; each gives one pair, pass 1 against pass 2 (10.6)."""
        return [cp for it in self.items for cp in it.checkpoints]

    @property
    def fits(self) -> bool:
        """Every Nightly rig ends inside the 4 h window (11.2)."""
        return all(v <= self.window_s for v in self.rig_load_s.values())


def _rotation_key(it: PlanItem) -> str:
    """A ladder rotates as a whole (its steps run in order, 2.5.1); every other item by its id."""
    return it.shard_key if it.kind in ("sweep", "split") else it.item_id


def plan_nightly(registry: Registry, day: date, rigs: Sequence[Rig], *, workspaces: Iterable[Workspace],
                 flows: Iterable[Flow] = (), entitlements: Iterable[str] = (), quarantine: Iterable[str] = (),
                 durations: Mapping[str, float] | None = None, max_client: tuple[int, int] = (1904, 1000),
                 ladders: Mapping[str, LadderDef] | None = None, frozen_available: bool = True,
                 canary_rig: str | None = None, aa_pass: bool = True, startups: Startups = Startups()) -> NightlyPlan:
    """The Nightly plan of `day` (10.6, 11.2; choices 13 and 14): every tier-1 item plus the night's rotating fifth
    of the rest of the main build's checkpoints, each captured twice in one session on one rig, packed over `rigs`
    (rigs 4 and 5) with the rig-5 work list on `canary_rig` (default the last rig). `aa_pass=False` is the calibration
    weekend, when only rig 5's canaries, fire drill and safety canaries run (1.6)."""
    if not rigs:
        raise ScopeConfigError("the Nightly run needs its rigs (11.2: rigs 4 and 5)")
    canary = canary_rig or rigs[-1].id
    if canary not in {r.id for r in rigs}:
        raise ScopeConfigError(f"canary rig {canary} is not a Nightly rig")
    rig5 = sum(s for _, s in NIGHTLY_RIG5_WORK_S)
    slot = day.toordinal() % NIGHTLY_ROTATION
    wmap = {w.id: w for w in workspaces}
    flow_map = {f.flow_id: f for f in flows if f.flow_class not in UNSCHEDULED_CLASSES}
    ctx = _Ctx("merge", registry, wmap, frozenset(), frozenset(), durations or {}, None, max_client, {}, ladders or {},
               frozen_available)
    items: list[PlanItem] = []
    cuts: list[Cut] = []
    if aa_pass:
        candidates, _ = _pass_items(ctx, "merge", ())
        candidates += [_flow_item(ctx, f) for f in sorted(flow_map.values(), key=lambda f: f.flow_id)]
        _pin_flows(candidates, flow_map)
        chosen = [it for it in candidates if any(cp.tier == 1 for cp in it.checkpoints)
                  or stable_hash(_rotation_key(it)) % NIGHTLY_ROTATION == slot]
        items, cuts = _common_filters(chosen, ctx, flow_map, frozenset(entitlements), frozenset(quarantine))
        items = sorted(items, key=_run_order)
    layout = shard_plan(items, rigs, None, startups, mode="aa", passes=NIGHTLY_PASSES, seed_s={canary: rig5})
    load = {r.id: layout.load_s(r.id) + (rig5 if r.id == canary else 0.0) for r in rigs}
    if not items:                                  # rig 5 still opens its session for the canaries (11.2)
        load[canary] = rig5 + startups.per_session_s
    return NightlyPlan(day, slot, items, {r.id: [it for it in items if r.id in it.rigs] for r in rigs}, canary,
                       NIGHTLY_RIG5_WORK_S, load, layout.shards(), cuts + ctx.excluded)


@dataclass
class CalibrationPlan:
    """A Calibration run's passes (3.9, 11.2): every checkpoint's passes on one rig, night by night."""
    kind: str                                 # weekend | emergency
    items: list[PlanItem]
    by_rig: dict[str, list[PlanItem]]
    passes: tuple[int, ...]                   # passes per night
    night_load_s: dict[str, tuple[float, ...]]
    starts: tuple[datetime, ...]
    deadline: datetime                        # every pass ends by this time
    start_deadline: datetime
    uncalibrated: tuple[str, ...]             # checkpoints this run leaves needs_human (uncalibrated)
    shards: list[Shard]

    def ends(self, rig: str) -> tuple[datetime, ...]:
        """When the rig's passes of each night end."""
        return tuple(s + timedelta(seconds=load) for s, load in zip(self.starts, self.night_load_s[rig]))

    @property
    def fits(self) -> bool:
        """Every night's passes end before the next night starts and the last by the deadline (all_quiet, 11.2)."""
        for rig in self.by_rig:
            ends = self.ends(rig)
            if any(e > nxt for e, nxt in zip(ends, self.starts[1:])) or (ends and ends[-1] > self.deadline):
                return False
        return True


def plan_calibration(scope: ScopePlan | Iterable[PlanItem], rigs: Sequence[Rig], kind: str, *, day: date,
                     durations: Mapping[str, float] | None = None, startups: Startups = Startups()) -> CalibrationPlan:
    """A weekend or emergency Calibration plan (3.9, 11.2; choice 15) over the items of `scope` (the full RC scope:
    every registered checkpoint). Weekend: `day` is the Friday; five passes per checkpoint back to back on one rig of
    `rigs` (rigs 1 to 4) from Friday 22:15 and a sixth on the same rig Saturday from 22:15, inside all_quiet (until
    Sunday 17:00), started by Saturday 02:00. Emergency (an unplanned fingerprint change): `day` is that night; tier-1
    items only, on the first three rigs, five passes from 22:15 ending by 06:00, started by 01:00; every other
    checkpoint stays `needs_human (reason: uncalibrated)` until the weekend run (`uncalibrated`)."""
    if kind not in CALIBRATION_KINDS:
        raise ValueError(f"unknown calibration kind {kind!r}: weekend or emergency")
    source = scope.items if isinstance(scope, ScopePlan) else list(scope)
    work = [replace(it, rigs=()) for it in source if it.kind != "soak"]        # the soak is the RC's own (1.9)
    left: tuple[str, ...] = ()
    if kind == "emergency":
        tier1 = [it for it in work if any(cp.tier == 1 for cp in it.checkpoints)]
        ones = {id(it) for it in tier1}
        left = tuple(cp.checkpoint_id for it in work if id(it) not in ones for cp in it.checkpoints)
        work, rigs, passes = tier1, list(rigs)[:EMERGENCY_RIGS], EMERGENCY_PASSES
        start = datetime.combine(day, CALIBRATION_START)
        starts = (start,)
        deadline = datetime.combine(day + timedelta(days=1), EMERGENCY_END)
        start_deadline = datetime.combine(day + timedelta(days=1), EMERGENCY_START_DEADLINE)
    else:
        if day.weekday() != 4:
            raise ValueError(f"a weekend Calibration run starts on a Friday, not {day:%A} (11.2)")
        passes = WEEKEND_PASSES
        starts = (datetime.combine(day, CALIBRATION_START), datetime.combine(day + timedelta(days=1), CALIBRATION_START))
        deadline = datetime.combine(day + timedelta(days=2), ALL_QUIET_END)
        start_deadline = datetime.combine(day + timedelta(days=1), WEEKEND_START_DEADLINE)
    if not rigs:
        raise ScopeConfigError("a Calibration run needs pool rigs (11.2: rigs 1 to 4)")
    layout = shard_plan(sorted(work, key=_run_order), rigs, durations, startups, mode="aa", passes=passes[0])
    loads: dict[str, tuple[float, ...]] = {}
    for r in rigs:
        cost = layout.cost(r.id)
        per_pass = cost.capture_s / passes[0]
        nights = [cost.total_s] + [per_pass * n + (startups.per_session_s + cost.switch_s if cost.sessions else 0.0)
                                   for n in passes[1:]]
        loads[r.id] = tuple(nights)
    kept = sorted(work, key=_run_order)
    return CalibrationPlan(kind, kept, {r.id: [it for it in kept if r.id in it.rigs] for r in rigs}, passes, loads,
                           starts, deadline, start_deadline, left, layout.shards())


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
