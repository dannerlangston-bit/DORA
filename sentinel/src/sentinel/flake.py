"""Flake statistics over a rolling 30 days (10.5 Flake statistics, 3.10 steps 5 and 6, 9.11 Flake rate).

The Nightly run refreshes `flake_stats` per checkpoint: captures compared (observations), re-captures, unstable
outcomes (in total and in the last 7 days), verdict flips between runs of one build (in total and the most on any one
build), step retries and agent fallbacks. The flake score is unstable outcomes plus verdict flips over observations;
the dashboard flake rate is the same ratio over every checkpoint compared (10.5).

- `collect(store, today)` reads the window and returns one `FlakeStats` per checkpoint, with the per-build counts the
  quarantine triggers of 10.5 need (`quarantine.evaluate`); it writes nothing.
- `refresh(store, today)` is `collect` plus the `flake_stats` write (`Store.replace_flake_stats`). It sets only the
  statistics columns, so the quarantine mirror (`quarantine.mirror`) is left as it is; a checkpoint whose window
  emptied gets zeros.
- `window_events(store, today)`: the step outcome rows of the window's runs (`records.StepEvent`), the input of the
  per-flow agent-fallback rate (11.5).
- `flake_rate(stats)` and `run_flake_rate(outcomes)`: the dashboard flake rate per 30 days and per run (3.10, 9.11).

Choices where the spec is ambiguous (reported to the coordinator):

1. An observation (10.5: "one compared capture of the checkpoint on one build") is one non-synthetic outcome row
   whose capture was compared: `identical`, `noise`, `intended`, `collateral`, `needs_human`,
   `invariant_violation`, `assertion_failed`, and `not_settled` with reason `unstable` or `aa_drift`. Rows that never
   reached a comparison (`not_run`, `env_error`, `harness_error`, `not_comparable`, `landmark_missing`, other
   `not_settled` reasons, `needs_baseline`, `unregistered_surface`) are not observations.
2. An unstable outcome is a run in which the checkpoint was `not_settled (reason: unstable)` or wrote a
   `flake_events` row (3.10 step 5), counted once per run and checkpoint.
3. A verdict flip: the verdict of a compared row is `clean` (`identical`, `noise`) or `changed` (every other compared
   outcome). Between consecutive runs on the same build pair (candidate and base build), a change of verdict is one
   flip, charged to the later run and to the candidate build. A Nightly `not_settled (reason: aa_drift)` row is one
   flip between the two captures of one build (3.10 "Nightly A/A drift hits"); it is how the merge run and the
   Nightly A/A pass disagree on one build (10.5), since the two runs compare different pairs. `not_settled` rows are
   not part of the verdict sequence (they are counted as unstable or as an A/A flip).
4. A row's build is the candidate build of its run's build pair; a run with no pair (the Nightly) takes the build of
   its candidate-side fingerprint, and a run with neither counts as a build of its own (it never pairs for a flip).
5. Window: a row belongs to the window when its run started (UTC date) on one of the 30 days ending `today`, and to
   the 7-day count on one of the 7 days ending `today`.
6. Step retries are step outcome rows of the checkpoint with `passed_after_retry` or an attempt above 1; agent
   fallbacks are rows with `passed_via_agent_fallback`. Fire-drill rows (`drill = 1`) are left out (6.5).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .records import CheckpointOutcome, StepEvent, now
from .store.store import Store

WINDOW_DAYS = 30                                   # 10.5 rolling window
RECENT_DAYS = 7                                    # 10.5: 3 unstable outcomes in 7 days
CLEAN = frozenset({"identical", "noise"})
COMPARED = frozenset({"identical", "noise", "intended", "collateral", "needs_human", "invariant_violation",
                      "assertion_failed"})
UNSTABLE, AA_DRIFT = "unstable", "aa_drift"        # the not_settled reasons that are observations


@dataclass(frozen=True)
class BuildCounts:
    """One checkpoint's counts on one build inside the window (the 10.5 per-build triggers)."""

    observations: int = 0
    unstable: int = 0
    flips: int = 0

    @property
    def score(self) -> float:
        return (self.unstable + self.flips) / self.observations if self.observations else 0.0


@dataclass(frozen=True)
class FlakeStats:
    """One checkpoint's rolling 30-day flake statistics (10.5, 8.3 `flake_stats`), with its per-build counts and the
    checkpoint facts the quarantine triggers read (`flow_class` `canary` is the exempt drill and safety canaries)."""

    checkpoint_id: str
    tier: int
    data_mode: str
    flow_id: str | None = None
    flow_class: str | None = None
    observations: int = 0
    recaptures: int = 0
    unstable_total: int = 0
    unstable_7d: int = 0
    flip_total: int = 0
    flips_same_build: int = 0
    step_retries: int = 0
    agent_fallbacks: int = 0
    last_flip_run_id: str | None = None
    by_build: Mapping[str, BuildCounts] = field(default_factory=dict)
    quarantined: bool = False

    @property
    def flake_score(self) -> float:
        """Unstable outcomes plus verdict flips over observations (10.5); 0 with no observation."""
        return (self.unstable_total + self.flip_total) / self.observations if self.observations else 0.0

    def row(self, updated_at: str | None = None) -> dict[str, Any]:
        """The statistics columns of the `flake_stats` row (8.3); the quarantine columns are the mirror's."""
        return {"checkpoint_id": self.checkpoint_id, "observations": self.observations, "recaptures": self.recaptures,
                "unstable_total": self.unstable_total, "unstable_7d": self.unstable_7d, "flip_total": self.flip_total,
                "flips_same_build": self.flips_same_build, "step_retries": self.step_retries,
                "agent_fallbacks": self.agent_fallbacks, "flake_score": round(self.flake_score, 6),
                "last_flip_run_id": self.last_flip_run_id, "updated_at": updated_at or now()}


def window(today: date, days: int = WINDOW_DAYS) -> tuple[str, str]:
    """The first and last UTC dates (ISO text) of the `days` days ending `today`."""
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


@dataclass(frozen=True)
class _Run:
    run_id: str
    day: str
    started_at: str
    build: str
    base: str


def _runs(store: Store, today: date) -> dict[str, _Run]:
    """The runs that started inside the window, with their candidate and base builds (choice 4)."""
    first, last = window(today)
    rows = store.query(
        "SELECT r.run_id, r.started_at, bp.candidate_build_id AS cand, bp.base_build_id AS base, "
        "(SELECT f.build_id FROM env_fingerprints f WHERE f.run_id = r.run_id AND f.side = 'candidate' "
        " ORDER BY f.fingerprint_id LIMIT 1) AS fp_build "
        "FROM runs r LEFT JOIN build_pairs bp ON bp.pair_id = r.pair_id "
        "WHERE substr(r.started_at, 1, 10) BETWEEN ? AND ?", (first, last))
    out = {}
    for r in rows:
        build = r["cand"] or r["fp_build"] or f"run:{r['run_id']}"
        out[r["run_id"]] = _Run(r["run_id"], r["started_at"][:10], r["started_at"], build, r["base"] or build)
    return out


@dataclass
class _Acc:
    observations: int = 0
    recaptures: int = 0
    unstable: set[str] = field(default_factory=set)
    unstable_7d: set[str] = field(default_factory=set)
    flips: list[tuple[str, str, str]] = field(default_factory=list)       # (started_at, run_id, build)
    obs_by_build: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    unstable_by_build: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    retries: int = 0
    fallbacks: int = 0


def collect(store: Store, today: date) -> list[FlakeStats]:
    """Every checkpoint's flake statistics for the 30 days ending `today` (module docstring); writes nothing."""
    runs = _runs(store, today)
    recent = window(today, RECENT_DAYS)[0]
    acc: dict[str, _Acc] = defaultdict(_Acc)
    sequences: dict[tuple[str, str, str], list[tuple[str, str, str]]] = defaultdict(list)
    span = window(today)
    inside = "JOIN runs r ON r.run_id = x.run_id WHERE substr(r.started_at, 1, 10) BETWEEN ? AND ?"
    for r in store.query(f"SELECT x.run_id, x.checkpoint_id, x.outcome, x.reason, x.recaptures FROM outcomes x "
                         f"{inside} AND x.synthetic = 0", span):
        run, cid = runs[r["run_id"]], r["checkpoint_id"]
        unstable = r["outcome"] == "not_settled" and r["reason"] == UNSTABLE
        drift = r["outcome"] == "not_settled" and r["reason"] == AA_DRIFT
        if not (r["outcome"] in COMPARED or unstable or drift):
            continue
        a = acc[cid]
        a.observations += 1
        a.recaptures += r["recaptures"] or 0
        a.obs_by_build[run.build] += 1
        if unstable:
            _unstable(a, run, recent)
        elif drift:
            a.flips.append((run.started_at, run.run_id, run.build))
        else:
            verdict = "clean" if r["outcome"] in CLEAN else "changed"
            sequences[(cid, run.build, run.base)].append((run.started_at, run.run_id, verdict))
    for r in store.query(f"SELECT DISTINCT x.run_id, x.checkpoint_id FROM flake_events x {inside}", span):
        _unstable(acc[r["checkpoint_id"]], runs[r["run_id"]], recent)
    for r in store.query(f"SELECT x.checkpoint_id, x.step_outcome, x.attempt FROM step_events x {inside} "
                         f"AND x.checkpoint_id IS NOT NULL AND x.step_outcome IS NOT NULL AND x.drill = 0", span):
        a = acc[r["checkpoint_id"]]
        a.retries += int(r["step_outcome"] == "passed_after_retry" or r["attempt"] > 1)
        a.fallbacks += int(r["step_outcome"] == "passed_via_agent_fallback")
    for (cid, build, _), seq in sequences.items():
        seq.sort()
        for (_, _, before), (at, run_id, after) in zip(seq, seq[1:]):
            if before != after:
                acc[cid].flips.append((at, run_id, build))
    known = {r["checkpoint_id"]: r for r in store.query(
        "SELECT c.checkpoint_id, c.tier, c.data_mode, c.flow_id, c.flow_class, COALESCE(s.quarantined, 0) AS q "
        "FROM checkpoints c LEFT JOIN flake_stats s ON s.checkpoint_id = c.checkpoint_id")}
    present = {r[0] for r in store.query("SELECT checkpoint_id FROM flake_stats")}
    out = []
    for cid in sorted((set(acc) | present) & set(known)):        # step rows may name an unregistered id
        meta, a = known[cid], acc.get(cid, _Acc())
        flips_by_build: dict[str, int] = defaultdict(int)
        for _, _, build in a.flips:
            flips_by_build[build] += 1
        builds = set(a.obs_by_build) | set(a.unstable_by_build) | set(flips_by_build)
        by_build = {b: BuildCounts(a.obs_by_build.get(b, 0), len(a.unstable_by_build.get(b, ())),
                                   flips_by_build.get(b, 0)) for b in sorted(builds)}
        out.append(FlakeStats(
            cid, meta["tier"], meta["data_mode"], meta["flow_id"], meta["flow_class"], observations=a.observations,
            recaptures=a.recaptures, unstable_total=len(a.unstable), unstable_7d=len(a.unstable_7d),
            flip_total=len(a.flips), flips_same_build=max(flips_by_build.values(), default=0),
            step_retries=a.retries, agent_fallbacks=a.fallbacks,
            last_flip_run_id=max(a.flips)[1] if a.flips else None, by_build=by_build, quarantined=bool(meta["q"])))
    return out


def _unstable(a: _Acc, run: _Run, recent: str) -> None:
    a.unstable.add(run.run_id)
    a.unstable_by_build[run.build].add(run.run_id)
    if run.day >= recent:
        a.unstable_7d.add(run.run_id)


def refresh(store: Store, today: date, *, at: str | None = None) -> list[FlakeStats]:
    """The Nightly refresh (10.5, 3.10 step 5): `collect`, then each checkpoint's `flake_stats` statistics columns
    replaced; returns the statistics written."""
    stats = collect(store, today)
    store.replace_flake_stats(s.row(at) for s in stats)
    return stats


def window_events(store: Store, today: date) -> list[StepEvent]:
    """The step outcome rows (`step_outcome` set) of every run that started in the 30 days ending `today`, as
    `records.StepEvent`s: the input of the 30-day agent-fallback rate per flow (11.5)."""
    out: list[StepEvent] = []
    for run_id in sorted(_runs(store, today)):
        out.extend(e for e in store.step_events(run_id) if e.step_outcome is not None)
    return out


def flake_rate(stats: Iterable[FlakeStats]) -> float | None:
    """The dashboard flake rate over 30 days (10.5, 9.11): unstable outcomes plus verdict flips over observations,
    over every checkpoint compared; None when nothing was compared."""
    rows = list(stats)
    obs = sum(s.observations for s in rows)
    return sum(s.unstable_total + s.flip_total for s in rows) / obs if obs else None


def run_flake_rate(outcomes: Iterable[CheckpointOutcome]) -> float | None:
    """The flake rate of one run (3.10): its compared checkpoints that were unstable or drifted on the A/A pass, over
    its compared checkpoints; synthetic rows are left out. None when the run compared nothing."""
    compared = flaky = 0
    for o in outcomes:
        if o.synthetic:
            continue
        hit = o.outcome == "not_settled" and o.reason in (UNSTABLE, AA_DRIFT)
        if o.outcome in COMPARED or hit:
            compared += 1
            flaky += int(hit)
    return flaky / compared if compared else None

