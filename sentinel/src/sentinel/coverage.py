"""Coverage accounting: the surface x persona x entitlement matrix and the manifest coverage table.

Spec: 2.12 (rows, columns, the cell function, display rules, coverage percent, the 2 percent tier-1
quarantine ceiling, the manifest coverage table), 2.2.3 (synthetic `unregistered_surface` rows),
8.3 (`coverage_cells` columns), 9.2 (manifest reconciliation wording), 10.5 (quarantine).

A cell is exactly one of `covered`, `not_run` (with its reason) or `not_registered`. A cell never
reads "passed": a covered cell shows its worst usable outcome and count, a `not_run` cell its
reason verbatim.

Choices where the spec is ambiguous (each is reported to the coordinator):

1. Outcome records carry no login, so outcomes arrive grouped by the entitlement profile of the
   login that produced them (`outcomes` maps profile id to records).
2. When a cell has no checkpoint, the quarantine and budget branches of the cell function read
   the scope plan's cut list (`sentinel.scope.Cut`), persona-aware: a cut checkpoint counts for its
   persona's cell, a surface-level cut for every persona.
3. Ties in "most frequent" go to the 1.10 precedence order for blocking outcomes (`env_error`,
   `harness_error`, `not_run`, `not_comparable`, `not_settled`, `landmark_missing`) and to the 4.13
   order for `not_run` reasons, then to the reason text.
4. Blocked cells (`not_run (blocked:<outcome>)`) stay in the coverage denominator, which drops only
   `not_run (entitlement)` and `not_run (scope)` cells (2.12, 9.11); they are also counted apart
   for run health. Synthetic rows and synthetic (canary) outcome records count nowhere in percent.
5. The quarantine ceiling is measured in cells as 2.12 states (10.5 says checkpoints): tier-1 cells
   `not_run (quarantine)` over every tier-1 cell of a registered row; strictly above 2 percent is red.
6. Manifest coverage: an entry's covering checkpoints are every outcome on its declared surfaces;
   "a diff above the noise floor" is an outcome `intended`, `collateral` or `needs_human`, or a
   region diff above threshold. A covered entry with none of these is `intended_change_not_observed`
   when `must_be_observed` is true, as is one whose 3.12 task C answer is `not_visible` or `unsure`.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .records import PERSONAS, CheckpointOutcome, Finding
from .scope import Cut, Manifest, Registry, Surface

USABLE_WORST_FIRST = ("invariant_violation", "assertion_failed", "needs_baseline", "collateral",
                      "needs_human", "intended", "noise", "identical")          # the 1.10 precedence
BLOCKING_ORDER = ("env_error", "harness_error", "not_run", "not_comparable", "not_settled", "landmark_missing")
NOT_RUN_ORDER = ("entitlement", "budget", "scope", "quarantine", "step_failed", "base_step_failed",
                 "app_crash", "model_unavailable")
DENOMINATOR_EXCLUDED = frozenset({"entitlement", "scope"})
QUARANTINE_CEILING = 0.02
OBSERVED_OUTCOMES = frozenset({"intended", "collateral", "needs_human"})
UNOBSERVED_VISIBILITY = frozenset({"not_visible", "unsure"})


@dataclass(frozen=True)
class Profile:
    """An entitlement profile: one login's entitlements (2.12 columns; phase 1 margin and IRA)."""
    id: str
    entitlements: frozenset[str]


@dataclass(frozen=True)
class Cell:
    """One matrix cell, keyed by (surface id, persona, entitlement profile) (2.12)."""
    surface_id: str
    persona: str
    profile: str
    tier: int
    state: str                        # covered | not_run | not_registered
    reason: str | None = None         # not_run only
    worst: str | None = None          # covered only
    count: int = 0                    # usable checkpoints
    kinds: tuple[str, ...] = ()       # test kinds that produced usable checkpoints
    needs_baseline: int = 0           # usable checkpoints that are needs_baseline (the footnote count)

    @property
    def blocked(self) -> bool:
        return self.state == "not_run" and self.reason is not None and self.reason.startswith("blocked:")

    @property
    def display(self) -> str:
        """The printed cell: `identical (12)`, `not_run (scope)`, `blocked:env_error` or `not_registered`."""
        if self.state == "covered":
            return f"{self.worst} ({self.count})"
        if self.state == "not_registered":
            return "not_registered"
        return self.reason if self.blocked else f"not_run ({self.reason})"

    @property
    def in_denominator(self) -> bool:
        """Counts toward coverage percent: not entitlement, not scope, not a synthetic row."""
        if self.state == "not_registered":
            return False
        return not (self.state == "not_run" and self.reason in DENOMINATOR_EXCLUDED)

    def to_row(self, run_id: str) -> dict:
        """The `coverage_cells` row of 8.3."""
        return {"run_id": run_id, "surface_id": self.surface_id, "persona": self.persona, "entitlement": self.profile,
                "tier": self.tier, "state": self.state, "not_run_reason": self.reason,
                "worst_outcome": self.worst, "checkpoint_count": self.count}


@dataclass(frozen=True)
class TierCoverage:
    """Coverage percent for one tier with its printed denominator (2.12)."""
    tier: int
    covered: int
    denominator: int
    blocked: int
    needs_baseline_only: int          # covered for invariants, uncovered for comparison

    @property
    def percent(self) -> float | None:
        return None if self.denominator == 0 else 100.0 * self.covered / self.denominator


@dataclass(frozen=True)
class QuarantineShare:
    """Tier-1 cells `not_run (quarantine)`; above 2 percent run health is red and the RC gate blocks."""
    quarantined: int
    cells: int

    @property
    def share(self) -> float:
        return 0.0 if self.cells == 0 else self.quarantined / self.cells

    @property
    def red(self) -> bool:
        return self.share > QUARANTINE_CEILING


@dataclass
class CoverageMatrix:
    """The matrix of one run: every registry surface plus one synthetic row per unregistered surface."""
    run_id: str
    personas: tuple[str, ...]
    profiles: tuple[Profile, ...]
    rows: list[str]
    synthetic_rows: frozenset[str]
    cells: dict[tuple[str, str, str], Cell]

    def cell(self, surface_id: str, persona: str, profile: str) -> Cell:
        return self.cells[(surface_id, persona, profile)]

    def tier_coverage(self, tier: int) -> TierCoverage:
        cells = [c for c in self.cells.values() if c.tier == tier and c.surface_id not in self.synthetic_rows]
        return TierCoverage(tier, covered=sum(c.state == "covered" for c in cells),
                            denominator=sum(c.in_denominator for c in cells), blocked=sum(c.blocked for c in cells),
                            needs_baseline_only=sum(c.state == "covered" and c.needs_baseline == c.count for c in cells))

    def quarantine_share(self) -> QuarantineShare:
        cells = [c for c in self.cells.values() if c.tier == 1 and c.surface_id not in self.synthetic_rows]
        return QuarantineShare(sum(c.state == "not_run" and c.reason == "quarantine" for c in cells), len(cells))

    def to_rows(self) -> list[dict]:
        """One `coverage_cells` row per cell (stored for merge, RC and Nightly runs, 8.3)."""
        return [self.cells[(s, p, f.id)].to_row(self.run_id) for s in self.rows for p in self.personas for f in self.profiles]


def _reason_rank(reason: str) -> int:
    code = reason.partition(":")[0]
    return NOT_RUN_ORDER.index(code) if code in NOT_RUN_ORDER else len(NOT_RUN_ORDER)


def _cut_applies(cuts: Iterable[Cut], reason: str, surface_id: str, persona: str) -> bool:
    return any(c.reason == reason and c.surface_id == surface_id and c.persona in (None, persona) for c in cuts)


def cell_state(surface: Surface, persona: str, profile: Profile, checkpoints: Sequence[CheckpointOutcome],
               cuts: Sequence[Cut] = ()) -> Cell:
    """`cell(surface, persona, profile)` of 2.12 for a registered surface.

    `checkpoints` are this run's outcome records for the surface under a workspace carrying the
    persona and a login with the profile.
    """
    key = dict(surface_id=surface.id, persona=persona, profile=profile.id, tier=surface.tier)
    if not checkpoints:
        if not surface.entitlements <= profile.entitlements:
            reason = "entitlement"
        elif _cut_applies(cuts, "quarantine", surface.id, persona):
            reason = "quarantine"
        elif _cut_applies(cuts, "budget", surface.id, persona):
            reason = "budget"
        else:
            reason = "scope"
        return Cell(**key, state="not_run", reason=reason)
    usable = [o for o in checkpoints if o.outcome in USABLE_WORST_FIRST]
    if not usable:
        counts = Counter(o.outcome for o in checkpoints if o.outcome in BLOCKING_ORDER)
        blocking = min(BLOCKING_ORDER, key=lambda k: (-counts[k], BLOCKING_ORDER.index(k)))
        if blocking == "not_run":
            reasons = Counter(o.reason or "" for o in checkpoints if o.outcome == "not_run")
            reason = min(reasons, key=lambda r: (-reasons[r], _reason_rank(r), r))
            return Cell(**key, state="not_run", reason=reason)
        return Cell(**key, state="not_run", reason=f"blocked:{blocking}")
    worst = min(usable, key=lambda o: USABLE_WORST_FIRST.index(o.outcome)).outcome
    return Cell(**key, state="covered", worst=worst, count=len(usable),
                kinds=tuple(sorted({o.test_kind for o in usable if o.test_kind})),
                needs_baseline=sum(o.outcome == "needs_baseline" for o in usable))


def build_matrix(run_id: str, registry: Registry, profiles: Sequence[Profile],
                 outcomes: Mapping[str, Iterable[CheckpointOutcome]], cuts: Iterable[Cut] = (),
                 personas: Sequence[str] = PERSONAS) -> CoverageMatrix:
    """The coverage matrix of a run from its planned scope (cuts) and its outcome records (2.12).

    `outcomes` maps each profile id to the outcome records its login produced; `cuts` is
    `ScopePlan.cuts`. An `unregistered_surface` record adds a synthetic row, tier 1 until
    classified (2.2.3), whose cells are all `not_registered`.
    """
    cuts = list(cuts)
    grouped: dict[tuple[str, str, str], list[CheckpointOutcome]] = defaultdict(list)
    synthetic: set[str] = set()
    for prof_id, records in outcomes.items():
        for o in records:
            if o.synthetic:
                continue
            if o.outcome == "unregistered_surface":
                if o.surface_id not in registry:
                    synthetic.add(o.surface_id)
                continue
            grouped[(o.surface_id, o.persona, prof_id)].append(o)
    cells: dict[tuple[str, str, str], Cell] = {}
    for s in registry:
        for persona in personas:
            for prof in profiles:
                cells[(s.id, persona, prof.id)] = cell_state(s, persona, prof, grouped.get((s.id, persona, prof.id), []), cuts)
    for sid in sorted(synthetic):
        for persona in personas:
            for prof in profiles:
                cells[(sid, persona, prof.id)] = Cell(sid, persona, prof.id, 1, "not_registered")
    rows = [s.id for s in registry] + sorted(synthetic)
    return CoverageMatrix(run_id, tuple(personas), tuple(profiles), rows, frozenset(synthetic), cells)


# -- manifest coverage table (2.12, 9.2) ------------------------------------------------------

@dataclass(frozen=True)
class ManifestCoverageRow:
    """One manifest entry with the checkpoints on the surfaces it names."""
    entry_id: str
    checkpoint_ids: tuple[str, ...]
    status: str                       # verified_visible | not_observed | no_covering_checkpoint
    observed: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        """The reconciliation line of 9.2."""
        if self.status == "verified_visible":
            return f"{self.entry_id}: verified visible on {len(self.observed)} checkpoints"
        if self.status == "no_covering_checkpoint":
            return f"{self.entry_id}: no covering checkpoint"
        return f"{self.entry_id}: covering checkpoints {', '.join(self.checkpoint_ids)} showed no change above floor"


def _observed(o: CheckpointOutcome) -> bool:
    return o.outcome in OBSERVED_OUTCOMES or bool(o.diff and any(r.above_threshold for r in o.diff.regions))


def manifest_coverage(run_id: str, manifest: Manifest, outcomes: Iterable[CheckpointOutcome],
                      visibility: Mapping[str, str] | None = None) -> tuple[list[ManifestCoverageRow], list[Finding]]:
    """The manifest coverage table and its `intended_change_not_observed` findings (2.12).

    `visibility` maps entry ids to the 3.12 task C answer (`visible`, `not_visible`, `unsure`).
    A finding never changes a checkpoint outcome.
    """
    visibility = visibility or {}
    records = [o for o in outcomes if not o.synthetic]
    rows: list[ManifestCoverageRow] = []
    findings: list[Finding] = []
    for e in manifest.entries:
        names = set(e.declared_surfaces)
        covering = [o for o in records if o.surface_id in names]
        ids = tuple(sorted({o.checkpoint_id for o in covering}))
        if not covering:
            rows.append(ManifestCoverageRow(e.entry_id, (), "no_covering_checkpoint"))
            continue
        seen = tuple(sorted({o.checkpoint_id for o in covering if _observed(o)}))
        answer = visibility.get(e.entry_id)
        if seen and answer not in UNOBSERVED_VISIBILITY:
            rows.append(ManifestCoverageRow(e.entry_id, ids, "verified_visible", seen))
            continue
        rows.append(ManifestCoverageRow(e.entry_id, ids, "not_observed", seen))
        if e.must_be_observed:
            findings.append(Finding(
                finding_id=f"fnd-{run_id}-cov-{len(findings) + 1:03d}", run_id=run_id,
                kind="intended_change_not_observed", first_seen_run_id=run_id, manifest_entry_ref=e.entry_id,
                description=(f"{e.entry_id}: covering checkpoints {', '.join(ids)} showed no change above floor; a "
                             f"question for {manifest.author} and the feature owner, not a bug (2.12)"),
                evidence={"raw_values": {"checkpoints": ",".join(ids), "visibility": answer or "none"}}))
    return rows, findings
