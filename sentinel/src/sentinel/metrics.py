"""The metrics dashboard, kill criteria, the report header stamp and the phase exits (1.5 Success criteria and kill
criteria, 9.11 Metrics dashboard and kill criteria, 9.2 header and commit status mode, 11.9 Phases 1 to 3, 11.10 R19;
SD-87).

Every metric comes from SQLite over a selectable window, excludes synthetic items, and is stamped with the harness
commit, config commit, model id and prompt hash range of the window's runs, so a shift can be attributed (9.11):

- `Window`: `Window.rolling(days, now)`, `Window.since_last_release(store, now)` (the default: since the last
  release) and `Window.run(store, run_id)` (per run).
- `compute(store, window)` returns `Metrics`, one field per 9.11 row (false-positive rate with the detector-noise and
  undeclared-intent split, flake rate, quarantine share, time to triage with the p90 latency beside it, escapes per
  release, coverage per tier, canary pass rate per class, A/A noise floor, Reviewer quality and overturn rate,
  harness health with cost per run) plus the stamp. `Metrics.lines()` is the Metrics page, `to_csv(metrics)` the
  export.
- `header_line(store, now)` is the metrics line of the merge and RC report headers (9.4 item 1, 9.11), and
  `pr_header_fragment(store, now)` the PR comment's "false-positive rate (30 d): {x}%" (9.2, SD-87, R19).
- `kill_state(store, now)` reads what the 1.5 kill criteria need and `kill_criteria(state)` evaluates them, one
  `KillVerdict` per criterion; `stop_pr_comments` (above 50 percent after three releases) and
  `revert_informational` (above 35 percent for 10 business days, 9.2) are the flags the PR delivery reads.
- `phase_exit(store, phase, now=...)` returns each 11.9 exit item of Phase 1, 2 or 3 as pass, fail or not
  measurable, with its evidence; the 10.10 acceptance results are passed in (`AcceptanceResult`), and without Part B
  the item reads "partial: no real branch" (11.7 row 11).

Choices where the spec is ambiguous (each is reported):

1. Findings "shown to a human" (9.11): non-synthetic, not held (2.9), of a kind the 9.2 Unexpected section lists
   (`UNEXPECTED_KINDS`), and not suppressed by an inherited decision: a finding whose `decision_id` names a decision
   produced from another finding (`decisions.source_finding_id`) inherited it. The numerator's split: `intended` is
   undeclared intent; `noise`, `expected_behavior` and `agent_mistake` are detector noise.
2. Time to triage (9.11): a finding's end is its first human action, the first decision made from it or the first
   two-person proposal on it ("proposals count from proposal time"); an open finding runs to the window's end. The
   minutes are business minutes (`sla.BusinessClock`, 09:00 to 17:00 America/New_York on XNYS days), capped at 10
   per finding and summed per run; a merge "morning" sums the merge runs that started on one America/New_York date.
   A run type's figure is the mean over its runs (mornings for merge), the maximum printed beside it; the p90 latency
   is over the findings with an end, uncapped.
3. Flake rate: the 10.5 flake window is 30 days, so the window selects only its end date and the rate is
   `flake.flake_rate(flake.collect(store, end))`; a per-run window counts the run's compared checkpoints that were
   `not_settled (reason: unstable or aa_drift)` (`flake.run_flake_rate`).
4. Coverage per tier sums the cells of the window's runs: `coverage_cells` for merge, RC and Nightly runs and the
   summary matrix (`summary_json.coverage.cells`) for PR runs; `not_registered`, `not_run (entitlement)` and
   `not_run (scope)` cells leave the denominator (2.12, `coverage.Cell.in_denominator`). The phase exits read merge
   runs only (11.9: "two consecutive weeks of merge runs").
5. Canary pass rate: `canary_results` rows of kind `mutant` created in the window, detected (state `detected`) over
   injected (every state but `generation_failed`), per defect class, for money cells and for the rest.
6. A/A noise floor: the current floors (`superseded_at` NULL) of the newest rendering key (7.13); the fix list is
   the frozen checkpoints with `nonzero_on_frozen`.
7. Reviewer quality: the newest `reviewer_evals` row per model id and prompt hash run in the window (held-out
   split, 3.14). The gate values are read from `metrics_json` through `EVAL_PATHS` (the first path present wins), the
   layout `reviewer.evals` writes: per-verdict precision and recall and the tier-1 mutant recall of
   `does_not_match_manifest`; "`intended` precision" (1.5) is `matches_manifest` precision (3.14). The overturn rate
   is decisions revoked over decisions made in the window (9.11 counting rule).
8. Harness health counts finished runs of the window (aborted_harness plus aborted_budget over runs), non-synthetic
   outcome rows (env_error over outcomes) and step outcome rows outside the fire drill (passed_via_agent_fallback
   over steps); cost per run is the mean of rig-minutes (shard sessions) and of model dollars (`model_usage`).
9. Kill criteria (1.5): "after three releases" is three released builds (`builds.released_at`); the 10-business-day
   revert reads the rolling 30-day rate at the end of each of the last 10 business days; per-release figures use the
   window between consecutive releases (the flake rate the 30 days ending each release). The flake rule's "after a
   fix list" and the A/A floor's 5-business-day quarantine are not read here (the latter is `quarantine.evaluate`).
10. Phase exits: "the last two weeks" is the 14 days ending `now`, the budgets checked week by week (each week needs
    a finished PR and merge run); "two consecutive days" of acceptance are two consecutive XNYS business days with
    both forms passed; ten fingerprint reads per pool rig (the newest ten of the window) must be identical; items no
    store row can show (flows with assertions, the Recorder share, sign-off, Explorer budgets, the recorded kill
    evaluation, no reappearing disposed diff, the sweep reproduction) are `not_measurable` with the reason.
"""
from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import pairwise
from typing import Any

from . import calendar as cal
from . import flake
from .coverage import DENOMINATOR_EXCLUDED, QUARANTINE_CEILING, QuarantineShare
from .quarantine import tier1_quarantine_share
from .sla import BusinessClock
from .store import Store
from .store.durations import percentile

UNEXPECTED_KINDS = frozenset({                   # the 9.2 Unexpected kind order (choice 1)
    "assertion_failed", "invariant_violation", "collateral", "needs_human", "landmark_missing", "unregistered_surface",
    "needs_baseline", "locator_failure", "dependency_map_gap", "manifest_missing", "unknown_popup", "redaction_miss"})
NOISE_STATES = ("noise", "expected_behavior", "agent_mistake")   # detector noise (choice 1)
INTENT_STATES = ("intended",)                                     # undeclared intent
TRIAGE_CAP_MIN = 10.0                                             # 9.11: capped at 10 minutes per finding
TRIAGE_TARGET_MIN = {"pr": (15.0, 10.0), "merge": (30.0, 20.0), "rc": (120.0, 120.0)}   # 1.5 Phase 1 / Phase 2
FP_TARGET = (0.35, 0.20)                                          # 1.5 Phase 1 / Phase 2 exit
FLAKE_TARGET = (0.05, 0.02)
ABORT_TARGET = (0.10, 0.05)
ENV_ERROR_TARGET = (0.03, 0.01)
COVERAGE_TARGET = ({1: 1.0, 2: 0.70}, {1: 1.0, 2: 0.90, 3: 0.60})
CANARY_OTHER_TARGET = 0.95
FALLBACK_TARGET = 0.05                                            # 11.9 Phase 3: fallback rate per flow
RUN_BUDGET_MIN = {"pr": 45.0, "merge": 180.0, "rc": 480.0}        # 11.2
FP_KILL, FP_REVERT, FP_REVERT_DAYS, KILL_RELEASES = 0.50, 0.35, 10, 3
FLAKE_KILL, TRIAGE_KILL_FACTOR, INTENDED_KILL, FREEZE_RUNS = 0.10, 2.0, 0.85, 2
EVAL_PATHS: Mapping[str, tuple[tuple[str, ...], ...]] = {         # choice 7
    "matches_manifest_precision": (("precision", "matches_manifest"), ("matches_manifest", "precision"),
                                   ("per_verdict", "matches_manifest", "precision")),
    "does_not_match_manifest_recall": (("recall", "does_not_match_manifest"), ("does_not_match_manifest", "recall"),
                                       ("per_verdict", "does_not_match_manifest", "recall")),
    "does_not_match_manifest_tier1_recall": (("tier1_recall", "does_not_match_manifest"),
                                             ("tier1_mutant_recall",), ("tier1_recall",))}
EVAL_GATES = (("matches_manifest_precision", 0.95), ("does_not_match_manifest_tier1_recall", 0.98),
              ("does_not_match_manifest_recall", 0.95))
PASS, FAIL, NOT_MEASURABLE = "pass", "fail", "not_measurable"
PARTIAL_NO_BRANCH = "partial: no real branch"                     # 11.7 row 11


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


# -- windows and rates ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    """A metrics window: (start, end] in UTC, or one run (`run_id`)."""

    start: datetime
    end: datetime
    run_id: str | None = None
    label: str = ""

    @classmethod
    def rolling(cls, days: int, now: datetime | None = None) -> Window:
        end = _now(now)
        return cls(end - timedelta(days=days), end, label=f"{days} d")

    @classmethod
    def since_last_release(cls, store: Store, now: datetime | None = None) -> Window:
        """9.11's default window: since the last release (the newest `released_at` up to `now`); 30 days when no
        build has been released."""
        end = _now(now)
        row = store.query("SELECT MAX(released_at) FROM builds WHERE released_at IS NOT NULL AND released_at <= ?",
                          (_iso(end),))
        if not row or row[0][0] is None:
            return cls.rolling(30, end)
        return cls(_t(row[0][0]), end, label="since the last release")

    @classmethod
    def run(cls, store: Store, run_id: str) -> Window:
        """One run: its findings, outcomes and steps (9.11 "per run")."""
        row = store.query("SELECT started_at, finished_at FROM runs WHERE run_id = ?", (run_id,))
        if not row:
            raise ValueError(f"unknown run {run_id}")
        start = _t(row[0]["started_at"])
        end = _t(row[0]["finished_at"]) if row[0]["finished_at"] else datetime.now(timezone.utc)
        return cls(start - timedelta(seconds=1), end, run_id, f"run {run_id}")

    def runs(self, alias: str = "r") -> tuple[str, tuple]:
        """A WHERE fragment selecting the window's runs on the `runs` table alias."""
        if self.run_id is not None:
            return f"{alias}.run_id = ?", (self.run_id,)
        return f"{alias}.started_at > ? AND {alias}.started_at <= ?", (_iso(self.start), _iso(self.end))

    def rows(self, column: str, run_column: str = "run_id") -> tuple[str, tuple]:
        """A WHERE fragment on a row's time column, or on its run for a run window."""
        if self.run_id is not None:
            return f"{run_column} = ?", (self.run_id,)
        return f"{column} > ? AND {column} <= ?", (_iso(self.start), _iso(self.end))


@dataclass(frozen=True)
class Rate:
    """A counted ratio with its printed denominator (9.11: "denominators print")."""

    num: int
    den: int

    @property
    def value(self) -> float | None:
        return self.num / self.den if self.den else None

    def pct(self, digits: int = 1) -> str:
        return "n/a" if self.value is None else f"{100 * self.value:.{digits}f}%"

    def text(self) -> str:
        return f"{self.pct()} ({self.num} of {self.den})"


# -- the 9.11 rows ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FalsePositive:
    """9.11 false-positive rate with the numerator split (choice 1)."""

    rate: Rate
    noise: int
    intent: int


def _shown_findings(store: Store, w: Window) -> list[Any]:
    clause, args = w.rows("f.created_at", "f.run_id")
    kinds = ",".join("?" * len(UNEXPECTED_KINDS))
    return store.query(
        "SELECT f.finding_id, f.run_id, f.triage_state, f.created_at FROM findings f LEFT JOIN decisions d ON "
        f"d.decision_id = f.decision_id WHERE f.synthetic = 0 AND f.held = 0 AND f.kind IN ({kinds}) AND {clause} AND "
        "NOT (f.decision_id IS NOT NULL AND (d.source_finding_id IS NULL OR d.source_finding_id <> f.finding_id))",
        (*sorted(UNEXPECTED_KINDS), *args))


def false_positive(store: Store, w: Window) -> FalsePositive:
    rows = _shown_findings(store, w)
    noise = sum(r["triage_state"] in NOISE_STATES for r in rows)
    intent = sum(r["triage_state"] in INTENT_STATES for r in rows)
    return FalsePositive(Rate(noise + intent, len(rows)), noise, intent)


@dataclass(frozen=True)
class TriageType:
    """Time to triage of one run type (choice 2): units are runs, or merge mornings."""

    run_type: str
    units: int
    mean_min: float | None
    max_min: float | None
    p90_latency_min: float | None


def _first_actions(store: Store) -> dict[int, datetime]:
    out: dict[int, datetime] = {}
    for sql in ("SELECT source_finding_id AS f, MIN(decided_at) AS at FROM decisions WHERE source_finding_id IS NOT "
                "NULL GROUP BY source_finding_id", "SELECT finding_id AS f, MIN(proposed_at) AS at FROM proposals "
                "GROUP BY finding_id"):
        for r in store.query(sql):
            at = _t(r["at"])
            out[r["f"]] = min(out.get(r["f"], at), at)
    return out


def triage(store: Store, w: Window, clock: BusinessClock = BusinessClock()) -> tuple[TriageType, ...]:
    """Time to triage per run type over the window (9.11, choice 2)."""
    first = _first_actions(store)
    runs = {r["run_id"]: (r["run_type"], r["started_at"]) for r in store.query("SELECT run_id, run_type, started_at "
                                                                               "FROM runs")}
    units: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    latency: dict[str, list[float]] = defaultdict(list)
    for f in _shown_findings(store, w):
        run_type, started = runs[f["run_id"]]
        created, done = _t(f["created_at"]), first.get(f["finding_id"])
        end = done if done is not None else w.end
        minutes = clock.elapsed(created, end).total_seconds() / 60
        unit = cal.ny(_t(started)).date().isoformat() if run_type == "merge" else f["run_id"]
        units[run_type][unit] += min(minutes, TRIAGE_CAP_MIN)
        if done is not None:
            latency[run_type].append(minutes)
    out = []
    for rt in cal.RUN_TYPES:
        if rt not in units:
            continue
        vals = list(units[rt].values())
        out.append(TriageType(rt, len(vals), sum(vals) / len(vals), max(vals),
                              percentile(latency[rt], 0.9) if latency[rt] else None))
    return tuple(out)


@dataclass(frozen=True)
class Escapes:
    """Escape rows per release (9.11, 10.9 G+1): counts per class, covered-missed on tier 1 apart."""

    by_release: Mapping[str, Mapping[str, int]]
    covered_tier1: Mapping[str, int]


def escapes(store: Store, w: Window) -> Escapes:
    if w.run_id is not None:
        return Escapes({}, {})
    clause, args = w.rows("entered_at")
    by: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    t1: dict[str, int] = defaultdict(int)
    for r in store.query(f"SELECT release, escape_class, tier FROM escapes WHERE {clause} ORDER BY release", args):
        by[r["release"]][r["escape_class"]] += 1
        if r["escape_class"] == "covered_missed" and r["tier"] == 1:
            t1[r["release"]] += 1
    return Escapes({k: dict(v) for k, v in by.items()}, dict(t1))


def _cells(store: Store, w: Window, run_types: Sequence[str] = cal.RUN_TYPES) -> list[Mapping[str, Any]]:
    """Every coverage cell of the window's runs of `run_types` (choice 4)."""
    clause, args = w.runs()
    marks = ",".join("?" * len(run_types))
    out: list[Mapping[str, Any]] = [dict(r) for r in store.query(
        f"SELECT c.* FROM coverage_cells c JOIN runs r ON r.run_id = c.run_id WHERE {clause} AND "
        f"r.run_type IN ({marks})", (*args, *run_types))]
    if "pr" in run_types:
        for r in store.query(f"SELECT r.summary_json FROM runs r WHERE {clause} AND r.run_type = 'pr' AND "
                             "r.summary_json IS NOT NULL", args):
            out += (json.loads(r["summary_json"]).get("coverage") or {}).get("cells") or ()
    return out


def _tier_rates(cells: Iterable[Mapping[str, Any]]) -> dict[int, Rate]:
    counts: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for c in cells:
        excluded = c["state"] == "not_registered" or (c["state"] == "not_run" and
                                                      c.get("not_run_reason") in DENOMINATOR_EXCLUDED)
        if excluded:
            continue
        counts[int(c["tier"])][1] += 1
        counts[int(c["tier"])][0] += c["state"] == "covered"
    return {t: Rate(*counts[t]) for t in sorted(counts)}


def coverage(store: Store, w: Window, run_types: Sequence[str] = cal.RUN_TYPES) -> dict[int, Rate]:
    """Covered cells over registered cells per tier over the window's runs (2.12, 9.11, choice 4)."""
    return _tier_rates(_cells(store, w, run_types))


@dataclass(frozen=True)
class Canary:
    """Canary pass rate (choice 5): per defect class, money cells and the rest, and per night."""

    by_class: Mapping[str, Rate]
    money: Rate
    other: Rate
    nights: tuple[tuple[str, Rate, Rate], ...]     # (run_id, money, other) per Nightly run, oldest first


def canary(store: Store, w: Window) -> Canary:
    clause, args = w.rows("c.created_at", "c.run_id")
    rows = store.query(f"SELECT c.run_id, c.defect_class, c.money_cell, c.state, r.started_at FROM canary_results c "
                       f"JOIN runs r ON r.run_id = c.run_id WHERE c.kind = 'mutant' AND c.state <> 'generation_failed' "
                       f"AND {clause} ORDER BY r.started_at, c.run_id", args)
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    money, other = [0, 0], [0, 0]
    nights: dict[str, list[list[int]]] = {}
    for r in rows:
        hit = int(r["state"] == "detected")
        night = nights.setdefault(r["run_id"], [[0, 0], [0, 0]])[0 if r["money_cell"] else 1]
        for acc in (by[r["defect_class"]], money if r["money_cell"] else other, night):
            acc[0] += hit
            acc[1] += 1
    return Canary({k: Rate(*v) for k, v in sorted(by.items())}, Rate(*money), Rate(*other),
                  tuple((k, Rate(*v[0]), Rate(*v[1])) for k, v in nights.items()))


@dataclass(frozen=True)
class AAFloor:
    """A/A noise floor of the current rendering key (choice 6)."""

    rendering_key: str | None
    checkpoints: int
    frozen_checkpoints: int
    fix_list: tuple[str, ...]
    masked_live_regions: int


def aa_floor(store: Store) -> AAFloor:
    key = store.query("SELECT rendering_key_sha256 FROM calibration_floors WHERE superseded_at IS NULL ORDER BY "
                      "valid_from DESC, floor_id DESC LIMIT 1")
    if not key:
        return AAFloor(None, 0, 0, (), 0)
    rows = store.query("SELECT f.checkpoint_id, f.nonzero_on_frozen, c.data_mode FROM calibration_floors f JOIN "
                       "checkpoints c ON c.checkpoint_id = f.checkpoint_id WHERE f.superseded_at IS NULL AND "
                       "f.rendering_key_sha256 = ?", (key[0][0],))
    frozen = {r["checkpoint_id"] for r in rows if r["data_mode"] == "frozen"}
    fixes = tuple(sorted({r["checkpoint_id"] for r in rows if r["nonzero_on_frozen"]}))
    return AAFloor(key[0][0], len({r["checkpoint_id"] for r in rows}), len(frozen), fixes,
                   sum(r["data_mode"] == "masked_live" for r in rows))


def eval_value(metrics: Mapping[str, Any], name: str) -> float | None:
    """One Reviewer gate value from an eval's `metrics_json` (choice 7), None when absent."""
    for path in EVAL_PATHS[name]:
        node: Any = metrics
        for key in path:
            node = node.get(key) if isinstance(node, Mapping) else None
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            return float(node)
    return None


@dataclass(frozen=True)
class EvalRow:
    model_id: str
    prompt_hash: str
    items: int
    passed_gate: bool
    run_at: str
    metrics: Mapping[str, Any]


@dataclass(frozen=True)
class ReviewerQuality:
    """Reviewer quality (choice 7): the newest eval per model and prompt in the window, and the overturn rate."""

    evals: tuple[EvalRow, ...]
    overturn: Rate


def _eval_rows(store: Store, clause: str, args: tuple) -> tuple[EvalRow, ...]:
    rows = store.query(f"SELECT * FROM reviewer_evals WHERE {clause} ORDER BY run_at DESC, eval_id DESC", args)
    seen: dict[tuple[str, str], EvalRow] = {}
    for r in rows:
        seen.setdefault((r["model_id"], r["prompt_hash"]), EvalRow(r["model_id"], r["prompt_hash"], r["items"],
                                                                   bool(r["passed_gate"]), r["run_at"],
                                                                   json.loads(r["metrics_json"])))
    return tuple(seen.values())


def reviewer_quality(store: Store, w: Window) -> ReviewerQuality:
    clause, args = (("run_at > ? AND run_at <= ?", (_iso(w.start), _iso(w.end))))
    dclause, dargs = w.rows("decided_at")
    made = store.query(f"SELECT COUNT(*), COALESCE(SUM(status = 'revoked'), 0) FROM decisions WHERE {dclause}",
                       dargs)[0]
    return ReviewerQuality(_eval_rows(store, clause, args), Rate(made[1], made[0]))


@dataclass(frozen=True)
class Harness:
    """Harness health (choice 8), per run type and overall."""

    aborted: Mapping[str, Rate]
    env_error: Rate
    fallback: Rate
    rig_minutes_per_run: float | None
    model_usd_per_run: float | None


def harness(store: Store, w: Window) -> Harness:
    clause, args = w.runs()
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in store.query(f"SELECT r.run_type, r.outcome FROM runs r WHERE {clause} AND r.lifecycle = 'finished'", args):
        hit = int(r["outcome"] in ("aborted_harness", "aborted_budget"))
        for k in (r["run_type"], "all"):
            by[k][0] += hit
            by[k][1] += 1
    env = store.query(f"SELECT COUNT(*), COALESCE(SUM(o.outcome = 'env_error'), 0) FROM outcomes o JOIN runs r ON "
                      f"r.run_id = o.run_id WHERE o.synthetic = 0 AND {clause}", args)[0]
    steps = store.query(f"SELECT COUNT(*), COALESCE(SUM(e.step_outcome = 'passed_via_agent_fallback'), 0) FROM "
                        f"step_events e JOIN runs r ON r.run_id = e.run_id WHERE e.step_outcome IS NOT NULL AND "
                        f"e.drill = 0 AND {clause}", args)[0]
    cost = store.query(f"SELECT COUNT(*) AS n, (SELECT SUM((julianday(s.finished_at) - julianday(s.started_at)) "
                       f"* 1440) FROM shards s JOIN runs r ON r.run_id = s.run_id WHERE {clause} AND s.finished_at "
                       f"IS NOT NULL) "
                       f"AS rig, (SELECT SUM(u.cost_usd) FROM model_usage u JOIN runs r ON r.run_id = u.run_id WHERE "
                       f"{clause}) AS usd FROM runs r WHERE {clause}", (*args, *args, *args))[0]
    n = cost["n"]
    return Harness({k: Rate(*v) for k, v in by.items()}, Rate(env[1], env[0]), Rate(steps[1], steps[0]),
                   (cost["rig"] or 0.0) / n if n else None, (cost["usd"] or 0.0) / n if n else None)


@dataclass(frozen=True)
class Stamp:
    """What the window's numbers were produced by (9.11): harness and config commits, model ids, prompt hashes."""

    harness: tuple[str, ...]
    config: tuple[str, ...]
    models: tuple[str, ...]
    prompts: tuple[str, ...]

    def text(self) -> str:
        def span(vals: tuple[str, ...]) -> str:
            if not vals:
                return "none"
            return vals[0] if len(vals) == 1 else f"{vals[0]}..{vals[-1]} ({len(vals)})"
        return (f"harness {span(self.harness)}, config {span(self.config)}, model "
                f"{', '.join(self.models) or 'none'}, prompt hash {span(self.prompts)}")


def stamp(store: Store, w: Window) -> Stamp:
    clause, args = w.runs()
    def ordered(sql: str, a: tuple) -> tuple[str, ...]:
        return tuple(dict.fromkeys(r[0] for r in store.query(sql, a) if r[0]))
    runs = f"FROM runs r WHERE {clause} ORDER BY r.started_at, r.run_id"
    rclause, rargs = w.rows("v.created_at", "v.run_id")
    return Stamp(ordered(f"SELECT r.harness_sha {runs}", args), ordered(f"SELECT r.config_commit {runs}", args),
                 tuple(sorted(set(ordered(f"SELECT v.model_id FROM reviews v WHERE {rclause}", rargs)) |
                              set(ordered(f"SELECT u.model_id FROM model_usage u JOIN runs r ON r.run_id = u.run_id "
                                          f"WHERE {clause}", args)))),
                 ordered(f"SELECT v.prompt_hash FROM reviews v WHERE {rclause} ORDER BY v.created_at, v.review_id",
                         rargs))


def flake_rate(store: Store, w: Window) -> Rate:
    """Choice 3: the 30-day flake window ending at the window's end, or the run's own compared checkpoints."""
    if w.run_id is not None:
        hits = n = 0
        for r in store.query("SELECT outcome, reason FROM outcomes WHERE run_id = ? AND synthetic = 0", (w.run_id,)):
            hit = r["outcome"] == "not_settled" and r["reason"] in (flake.UNSTABLE, flake.AA_DRIFT)
            if r["outcome"] in flake.COMPARED or hit:
                n += 1
                hits += int(hit)
        return Rate(hits, n)
    stats = flake.collect(store, w.end.date())
    return Rate(sum(s.unstable_total + s.flip_total for s in stats), sum(s.observations for s in stats))


@dataclass(frozen=True)
class Metrics:
    """The 9.11 metrics of one window."""

    window: Window
    false_positive: FalsePositive
    flake: Rate
    quarantine: QuarantineShare
    triage: tuple[TriageType, ...]
    escapes: Escapes
    coverage: Mapping[int, Rate]
    canary: Canary
    aa_floor: AAFloor
    reviewer: ReviewerQuality
    harness: Harness
    stamp: Stamp

    def triage_of(self, run_type: str) -> TriageType | None:
        return next((t for t in self.triage if t.run_type == run_type), None)

    def rows(self) -> list[tuple[str, str, int | None, int | None, str]]:
        """(metric, scope, numerator, denominator, value) for the CSV export (9.11)."""
        def rate(name: str, scope: str, r: Rate) -> tuple[str, str, int | None, int | None, str]:
            return name, scope, r.num, r.den, "" if r.value is None else f"{r.value:.6f}"
        fp = self.false_positive
        out = [rate("false_positive_rate", "all", fp.rate),
               ("false_positive_detector_noise", "all", fp.noise, None, ""),
               ("false_positive_undeclared_intent", "all", fp.intent, None, ""),
               rate("flake_rate", "all", self.flake),
               rate("quarantine_share", "tier 1", Rate(self.quarantine.quarantined, self.quarantine.checkpoints))]
        for t in self.triage:
            out.append(("time_to_triage_min", t.run_type, t.units, None,
                        "" if t.mean_min is None else f"{t.mean_min:.2f}"))
            out.append(("triage_p90_latency_min", t.run_type, None, None,
                        "" if t.p90_latency_min is None else f"{t.p90_latency_min:.2f}"))
        for rel, classes in self.escapes.by_release.items():
            out += [("escapes", f"{rel} {k}", v, None, str(v)) for k, v in sorted(classes.items())]
        out += [rate("coverage", f"tier {t}", r) for t, r in self.coverage.items()]
        out += [rate("canary_pass_rate", "money cells", self.canary.money), rate("canary_pass_rate", "other",
                                                                                   self.canary.other)]
        out += [rate("canary_pass_rate", k, r) for k, r in self.canary.by_class.items()]
        a = self.aa_floor
        out.append(("aa_nonzero_on_frozen", "current key", len(a.fix_list), a.frozen_checkpoints, str(len(a.fix_list))))
        out.append(rate("reviewer_overturn_rate", "all", self.reviewer.overturn))
        out += [rate("aborted_share", k, r) for k, r in sorted(self.harness.aborted.items())]
        out += [rate("env_error_share", "all", self.harness.env_error), rate("fallback_share", "all",
                                                                              self.harness.fallback)]
        return out

    def lines(self) -> list[str]:
        """The Metrics page (9.11), stamped."""
        fp = self.false_positive
        out = [f"metrics ({self.window.label or 'window'}): {self.stamp.text()}",
               f"false-positive rate {fp.rate.text()}: detector noise {fp.noise}, undeclared intent {fp.intent}",
               f"flake rate {self.flake.text()}",
               f"quarantine share {Rate(self.quarantine.quarantined, self.quarantine.checkpoints).text()}"
               + (" (red: above 2 percent)" if self.quarantine.red else "")]
        for t in self.triage:
            out.append(f"time to triage {t.run_type}: {_minutes(t.mean_min)} mean over {t.units} "
                       f"{'mornings' if t.run_type == 'merge' else 'runs'}, max {_minutes(t.max_min)}, p90 latency to "
                       f"first decision {_minutes(t.p90_latency_min)}")
        for rel, classes in self.escapes.by_release.items():
            out.append(f"escapes {rel}: " + ", ".join(f"{v} {k}" for k, v in sorted(classes.items())))
        if self.coverage:
            out.append("coverage " + ", ".join(f"tier {t} {r.text()}" for t, r in self.coverage.items()))
        out.append(f"canary pass rate: money cells {self.canary.money.text()}, other {self.canary.other.text()}")
        a = self.aa_floor
        out.append(f"A/A floor: {a.checkpoints} checkpoints calibrated, {len(a.fix_list)} of {a.frozen_checkpoints} "
                   f"frozen non-zero (fix list), {a.masked_live_regions} masked_live regions recorded")
        for e in self.reviewer.evals:
            out.append(f"reviewer eval {e.model_id} prompt {e.prompt_hash}: {e.items} items, gate "
                       f"{'passed' if e.passed_gate else 'failed'} ({e.run_at})")
        out.append(f"reviewer overturn rate {self.reviewer.overturn.text()}")
        h = self.harness
        out.append(f"harness: aborted {h.aborted.get('all', Rate(0, 0)).text()} of runs, env_error "
                   f"{h.env_error.text()} of checkpoints, agent fallback {h.fallback.text()} of steps, cost per run "
                   f"{_num(h.rig_minutes_per_run, 'rig-min')} and {_num(h.model_usd_per_run, 'USD', 2)}")
        return out


def _minutes(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1f} min"


def _num(v: float | None, unit: str, digits: int = 0) -> str:
    return "n/a" if v is None else f"{v:.{digits}f} {unit}"


def compute(store: Store, window: Window, *, clock: BusinessClock = BusinessClock()) -> Metrics:
    """Every 9.11 metric over `window`, synthetic items excluded, stamped."""
    return Metrics(window, false_positive(store, window), flake_rate(store, window), tier1_quarantine_share(store),
                   triage(store, window, clock), escapes(store, window), coverage(store, window), canary(store, window),
                   aa_floor(store), reviewer_quality(store, window), harness(store, window), stamp(store, window))


def to_csv(metrics: Metrics) -> str:
    """The 9.11 CSV export: one row per metric and scope, the window and stamp in the first rows."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["metric", "scope", "numerator", "denominator", "value"])
    w.writerow(["window", metrics.window.label, "", "", f"{_iso(metrics.window.start)}..{_iso(metrics.window.end)}"])
    w.writerow(["stamp", "", "", "", metrics.stamp.text()])
    for row in metrics.rows():
        w.writerow(["" if v is None else v for v in row])
    return buf.getvalue()


def pr_header_fragment(store: Store, now: datetime | None = None) -> str:
    """The PR comment header's "false-positive rate (30 d): {x}%" (9.2, SD-87, R19)."""
    v = false_positive(store, Window.rolling(30, now)).rate.value
    return f"false-positive rate (30 d): {'n/a' if v is None else f'{100 * v:.1f}%'}"


def header_line(store: Store, now: datetime | None = None, window: Window | None = None) -> str:
    """The metrics line of the merge and RC report headers (9.4 item 1, 9.11): every metric of the window (default
    since the last release) in one line, then the stamp."""
    w = window or Window.since_last_release(store, now)
    m = compute(store, w)
    fp30 = false_positive(store, Window.rolling(30, w.end)).rate
    parts = [f"false-positive rate {m.false_positive.rate.pct()} (30 d {fp30.pct()})", f"flake {m.flake.pct()}",
             f"quarantine {Rate(m.quarantine.quarantined, m.quarantine.checkpoints).pct()}"]
    if m.triage:
        parts.append("triage " + ", ".join(f"{t.run_type} {_minutes(t.mean_min)}" for t in m.triage))
    if m.coverage:
        parts.append("coverage " + ", ".join(f"tier {t} {r.pct(0)}" for t, r in m.coverage.items()))
    parts += [f"canary money {m.canary.money.pct(0)}, other {m.canary.other.pct(0)}",
              f"escapes {sum(sum(c.values()) for c in m.escapes.by_release.values())}",
              f"aborted {m.harness.aborted.get('all', Rate(0, 0)).pct()}, env_error {m.harness.env_error.pct()}",
              m.stamp.text()]
    return f"metrics ({w.label}): " + " | ".join(parts)


# -- kill criteria (1.5, 9.2, 9.11) -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class KillState:
    """What the 1.5 kill criteria read (choice 9); per-release tuples are oldest first."""

    releases: int = 0
    fp_rate_30d: float | None = None
    fp_daily_30d: tuple[float | None, ...] = ()
    flake_by_release: tuple[float | None, ...] = ()
    quarantine_share: float = 0.0
    triage_over_2x_by_release: tuple[bool, ...] = ()
    covered_tier1_escapes_by_release: tuple[int, ...] = ()
    tier1_merge_coverage: float | None = None
    money_cell_misses: int = 0
    reviewer_intended_precision: float | None = None
    aborted_harness_7d: int = 0


@dataclass(frozen=True)
class KillVerdict:
    """One kill or fix criterion of 1.5: whether it fired, what happens, and the numbers read."""

    criterion: str
    fired: bool
    action: str
    evidence: str


def _last_two(values: Sequence[Any], test: Any) -> bool:
    return len(values) >= 2 and all(v is not None and test(v) for v in values[-2:])


def _f(v: float | None) -> str:
    return "n/a" if v is None else f"{100 * v:.1f}%"


def kill_criteria(state: KillState) -> tuple[KillVerdict, ...]:
    """Every 1.5 kill criterion evaluated on `state`, fired or not, in table order."""
    s = state
    stop = s.releases >= KILL_RELEASES and s.fp_rate_30d is not None and s.fp_rate_30d > FP_KILL
    revert_days = len(s.fp_daily_30d) >= FP_REVERT_DAYS and all(
        v is not None and v > FP_REVERT for v in s.fp_daily_30d[-FP_REVERT_DAYS:])
    return (
        KillVerdict("stop_pr_comments", stop, "PR comments stop, fix, re-enter pilot (1.5)",
                    f"false-positive rate (30 d) {_f(s.fp_rate_30d)} after {s.releases} releases (kill above 50 "
                    "percent after three)"),
        KillVerdict("revert_informational", revert_days or stop, "commit status back to informational, announced in "
                    "the release channel (9.2)", "rolling 30-day false-positive rate over the last 10 business days: "
                    + ", ".join(_f(v) for v in s.fp_daily_30d[-FP_REVERT_DAYS:]) + " (revert above 35 percent on all)"),
        KillVerdict("no_new_flows", _last_two(s.flake_by_release, lambda v: v > FLAKE_KILL),
                    "no new flows until the flake rate is under target (1.5)",
                    "flake rate of the last two releases: " + ", ".join(_f(v) for v in s.flake_by_release[-2:])),
        KillVerdict("rc_gate_quarantine", s.quarantine_share > QUARANTINE_CEILING, "run health red and the RC gate "
                    "blocks until flow owners clear rows (1.5, 10.5)",
                    f"tier-1 quarantine share {_f(s.quarantine_share)} (ceiling 2 percent)"),
        KillVerdict("cut_tier3_scope", _last_two(s.triage_over_2x_by_release, bool), "cut tier-3 scope, printed (1.5, "
                    "11.5)", "time to triage above 2x target in the last two releases: "
                    + ", ".join(str(v) for v in s.triage_over_2x_by_release[-2:])),
        KillVerdict("system_owner_review", _last_two(s.covered_tier1_escapes_by_release, lambda v: v > 0),
                    "system owner review of whether to continue (1.5)", "covered tier-1 escapes in the last two "
                    "releases: " + ", ".join(str(v) for v in s.covered_tier1_escapes_by_release[-2:])),
        KillVerdict("rc_gate_coverage", s.tier1_merge_coverage is not None and s.tier1_merge_coverage < 1.0,
                    "tier 1 below 100 percent in a merge run blocks the release's RC gate (1.5)",
                    f"tier-1 coverage of the latest merge run {_f(s.tier1_merge_coverage)}"),
        KillVerdict("detector_blind", s.money_cell_misses > 0, "canary state detector_blind, P1 page and the banner on "
                    "every report; no verdict trusted until a green canary (1.5, 10.3)",
                    f"{s.money_cell_misses} money-cell canary misses on the last Nightly run"),
        KillVerdict("intended_disabled", s.reviewer_intended_precision is not None and
                    s.reviewer_intended_precision < INTENDED_KILL, "intended disabled; declared-surface diffs go to "
                    "needs_human (reason: reviewer_unavailable) until a new eval passes (1.5)",
                    f"intended precision {s.reviewer_intended_precision} (kill below 0.85)"),
        KillVerdict("harness_freeze", s.aborted_harness_7d >= FREEZE_RUNS, "harness changes frozen until root-caused "
                    "(1.5, 9.10)", f"{s.aborted_harness_7d} aborted_harness runs in 7 days"),
    )


def fired(verdicts: Iterable[KillVerdict], criterion: str) -> bool:
    """True when the named criterion fired (`stop_pr_comments`, `revert_informational`, ...)."""
    return any(v.criterion == criterion and v.fired for v in verdicts)


def _release_windows(store: Store, now: datetime) -> list[tuple[str, datetime, datetime]]:
    rows = store.query("SELECT build_id, released_at FROM builds WHERE released_at IS NOT NULL AND released_at <= ? "
                       "ORDER BY released_at, build_id", (_iso(now),))
    out, prev = [], None
    for r in rows:
        at = _t(r["released_at"])
        out.append((r["build_id"], prev or at - timedelta(days=30), at))
        prev = at
    return out


def kill_state(store: Store, now: datetime | None = None, *, clock: BusinessClock = BusinessClock()) -> KillState:
    """`KillState` from the store at `now` (choice 9)."""
    end = _now(now)
    rels = _release_windows(store, end)
    days = cal.business_days(end.date() - timedelta(days=30), end.date())[-FP_REVERT_DAYS:]
    daily = tuple(false_positive(store, Window.rolling(30, min(end, cal.wall_time(d, time(23, 59))))).rate.value
                  for d in days)
    targets = {rt: t[0] for rt, t in TRIAGE_TARGET_MIN.items()}
    over = []
    for _, start, stop in rels:
        tt = triage(store, Window(start, stop), clock)
        over.append(any(t.mean_min is not None and t.mean_min > TRIAGE_KILL_FACTOR * targets.get(t.run_type, 1e9)
                        for t in tt))
    esc = escapes(store, Window(datetime.min.replace(tzinfo=timezone.utc), end))
    last_merge = store.query("SELECT run_id FROM runs WHERE run_type = 'merge' AND started_at <= ? ORDER BY started_at "
                             "DESC, run_id DESC LIMIT 1", (_iso(end),))
    t1 = coverage(store, Window.run(store, last_merge[0][0]), ("merge",)).get(1) if last_merge else None
    nightly = store.query("SELECT run_id FROM runs WHERE run_type = 'nightly' AND started_at <= ? ORDER BY started_at "
                          "DESC, run_id DESC LIMIT 1", (_iso(end),))
    misses = store.query("SELECT COUNT(*) FROM canary_results WHERE run_id = ? AND kind = 'mutant' AND money_cell = 1 "
                         "AND state = 'detector_blind'", (nightly[0][0],))[0][0] if nightly else 0
    evals = _eval_rows(store, "run_at <= ?", (_iso(end),))
    newest = max(evals, key=lambda e: e.run_at, default=None)
    aborted = store.query("SELECT COUNT(*) FROM runs WHERE outcome = 'aborted_harness' AND started_at > ? AND "
                          "started_at <= ?", (_iso(end - timedelta(days=7)), _iso(end)))[0][0]
    return KillState(
        releases=len(rels), fp_rate_30d=false_positive(store, Window.rolling(30, end)).rate.value, fp_daily_30d=daily,
        flake_by_release=tuple(flake_rate(store, Window(start, stop)).value for _, start, stop in rels),
        quarantine_share=tier1_quarantine_share(store).share, triage_over_2x_by_release=tuple(over),
        covered_tier1_escapes_by_release=tuple(esc.covered_tier1.get(b, 0) for b, _, _ in rels),
        tier1_merge_coverage=t1.value if t1 else None, money_cell_misses=misses,
        reviewer_intended_precision=eval_value(newest.metrics, "matches_manifest_precision") if newest else None,
        aborted_harness_7d=aborted)


def stop_pr_comments(store: Store, now: datetime | None = None) -> bool:
    """The 1.5 kill rule the PR delivery reads: above 50 percent after three releases, PR comments stop (R19)."""
    return fired(kill_criteria(kill_state(store, now)), "stop_pr_comments")


def revert_informational(store: Store, now: datetime | None = None) -> bool:
    """9.2: the commit status reverts to informational (above 35 percent for 10 business days, or the kill rule)."""
    return fired(kill_criteria(kill_state(store, now)), "revert_informational")


# -- phase exits (11.9) -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AcceptanceResult:
    """One day's 10.10 acceptance run: Part A (M12 mutant) and Part B (real branch, None when absent)."""

    day: date
    part_a: bool
    part_b: bool | None


@dataclass(frozen=True)
class Criterion:
    """One 11.9 exit item: pass, fail or not measurable, with what was measured."""

    item: str
    status: str
    evidence: str


@dataclass(frozen=True)
class PhaseExit:
    phase: int
    criteria: tuple[Criterion, ...]

    @property
    def passed(self) -> bool:
        return all(c.status == PASS for c in self.criteria)

    def lines(self) -> list[str]:
        return [f"phase {self.phase} exit: " + ("met" if self.passed else "not met")] + [
            f"  {c.status}: {c.item}: {c.evidence}" for c in self.criteria]


def _crit(item: str, ok: bool | None, evidence: str) -> Criterion:
    return Criterion(item, NOT_MEASURABLE if ok is None else (PASS if ok else FAIL), evidence)


def _below(item: str, rate: Rate, limit: float) -> Criterion:
    v = rate.value
    return _crit(item, None if v is None else v < limit, f"{rate.text()}, target under {100 * limit:.0f} percent")


def _acceptance(results: Sequence[AcceptanceResult]) -> Criterion:
    item = "acceptance test passes in both forms on two consecutive days (10.10)"
    if not results:
        return _crit(item, None, "no acceptance result recorded")
    if any(r.part_b is None for r in results):
        return _crit(item, None, PARTIAL_NO_BRANCH)
    good = sorted(r.day for r in results if r.part_a and r.part_b)
    days = cal.business_days(good[0], good[-1]) if good else ()
    pairs = any(a in good and b in good for a, b in pairwise(days))
    return _crit(item, pairs, f"both forms passed on {', '.join(map(str, good)) or 'no day'}")


def _fingerprints(store: Store, w: Window) -> Criterion:
    item = "fingerprint identical across ten launches per rig"
    clause, args = w.rows("f.read_at", "f.run_id")
    reads: dict[str, list[str]] = defaultdict(list)
    for r in store.query(f"SELECT f.rig_id, f.fingerprint_sha256 FROM env_fingerprints f JOIN rigs g ON g.rig_id = "
                         f"f.rig_id WHERE g.role = 'pool' AND {clause} ORDER BY f.read_at DESC, f.fingerprint_id DESC",
                         args):
        reads[r["rig_id"]].append(r["fingerprint_sha256"])
    if not reads:
        return _crit(item, None, "no fingerprint read in the window")
    short = sorted(k for k, v in reads.items() if len(v) < 10)
    if short:
        return _crit(item, None, f"fewer than ten reads on {', '.join(short)}")
    differ = sorted(k for k, v in reads.items() if len(set(v[:10])) > 1)
    return _crit(item, not differ, f"differs on {', '.join(differ)}" if differ else f"identical on {len(reads)} rigs")


def _floors(store: Store) -> Criterion:
    item = "A/A floor recorded for every census checkpoint and zero on frozen ones"
    a = aa_floor(store)
    census = {r[0] for r in store.query("SELECT checkpoint_id FROM checkpoints WHERE kind = 'census' AND "
                                        "retired_in_build IS NULL AND checkpoint_id NOT IN (SELECT renamed_from FROM "
                                        "checkpoints WHERE renamed_from IS NOT NULL)")}
    if not census:
        return _crit(item, None, "no census checkpoint registered")
    floored = {r[0] for r in store.query("SELECT DISTINCT checkpoint_id FROM calibration_floors WHERE superseded_at IS "
                                         "NULL AND rendering_key_sha256 = ?", (a.rendering_key,))}
    missing = sorted(census - floored)
    evidence = (f"{len(census) - len(missing)} of {len(census)} census checkpoints have a floor; "
                f"{len(a.fix_list)} frozen non-zero")
    return _crit(item, not missing and not a.fix_list, evidence)


def _budgets(store: Store, end: datetime) -> Criterion:
    item = "PR runs inside 45 minutes and merge runs inside 3 hours for two consecutive weeks"
    over, missing = [], []
    for k, (a, b) in enumerate(((end - timedelta(days=14), end - timedelta(days=7)), (end - timedelta(days=7), end))):
        rows = store.query("SELECT run_id, run_type, started_at, finished_at FROM runs WHERE run_type IN ('pr', "
                           "'merge') AND finished_at IS NOT NULL AND started_at > ? AND started_at <= ?",
                           (_iso(a), _iso(b)))
        for rt in ("pr", "merge"):
            mine = [r for r in rows if r["run_type"] == rt]
            if not mine:
                missing.append(f"week {k + 1} has no finished {rt} run")
            for r in mine:
                minutes = (_t(r["finished_at"]) - _t(r["started_at"])).total_seconds() / 60
                if minutes > RUN_BUDGET_MIN[rt]:
                    over.append(f"{rt} run {r['run_id']} took {minutes:.0f} min in week {k + 1}")
    if over:
        return _crit(item, False, "; ".join(over))
    return _crit(item, None if missing else True, "; ".join(missing) or "every PR and merge run inside its budget")


def _canaries(store: Store, w: Window, phase: int) -> Criterion:
    c = canary(store, w)
    item = ("canary detection 100 percent on money-cell mutants" if phase == 1 else
            "money-cell canaries at 100 percent and other classes at least 95 percent rolling")
    if c.money.den == 0:
        return _crit(item, None, "no money-cell canary in the window")
    ok = c.money.num == c.money.den and (phase == 1 or c.other.value is None or c.other.value >= CANARY_OTHER_TARGET)
    return _crit(item, ok, f"money cells {c.money.text()}, other {c.other.text()}")


def _drill(store: Store, end: datetime) -> Criterion:
    item = "fire drill passes seven consecutive nights with zero aborted_safety false alarms"
    nights = store.query("SELECT run_id, outcome FROM runs WHERE run_type = 'nightly' AND started_at <= ? ORDER BY "
                         "started_at DESC, run_id DESC LIMIT 7", (_iso(end),))
    if len(nights) < 7:
        return _crit(item, None, f"{len(nights)} Nightly runs; seven needed")
    bad = []
    for n in nights:
        rows = store.query("SELECT state FROM canary_results WHERE run_id = ? AND kind = 'fire_drill'", (n["run_id"],))
        if n["outcome"] == "aborted_safety" or not rows or any(r[0] != "detected" for r in rows):
            bad.append(n["run_id"])
    return _crit(item, not bad, f"failed on {', '.join(bad)}" if bad else "seven nights passed")


def _outcomes_complete(store: Store, w: Window) -> Criterion:
    item = "every checkpoint has exactly one outcome and every not_run prints its reason"
    clause, args = w.runs()
    runs = [r[0] for r in store.query(f"SELECT r.run_id FROM runs r WHERE {clause} AND r.lifecycle = 'finished'", args)]
    if not runs:
        return _crit(item, None, "no finished run in the window")
    marks = ",".join("?" * len(runs))
    missing = store.query(f"SELECT COUNT(*) FROM run_scope s WHERE s.run_id IN ({marks}) AND NOT EXISTS (SELECT 1 FROM "
                          "outcomes o WHERE o.run_id = s.run_id AND o.checkpoint_id = s.checkpoint_id AND "
                          "o.synthetic = 0)", runs)[0][0]
    silent = store.query(f"SELECT COUNT(*) FROM outcomes WHERE run_id IN ({marks}) AND outcome = 'not_run' AND (reason "
                         "IS NULL OR reason = '')", runs)[0][0]
    return _crit(item, missing == 0 and silent == 0, f"{len(runs)} finished runs: {missing} scoped checkpoints "
                                                     f"without an outcome, {silent} not_run without a reason")


def _triage_items(m: Metrics, phase: int) -> list[Criterion]:
    out = []
    for rt in ("pr", "merge"):
        t, limit = m.triage_of(rt), TRIAGE_TARGET_MIN[rt][phase - 1]
        unit = "per PR run" if rt == "pr" else "per merge morning"
        item = f"time to triage under {limit:.0f} minutes {unit}"
        if t is None or t.mean_min is None:
            out.append(_crit(item, None, f"no {rt} finding shown in the window"))
        else:
            out.append(_crit(item, t.mean_min < limit, f"mean {t.mean_min:.1f} min over {t.units}, max "
                                                       f"{_minutes(t.max_min)}"))
    return out


def _coverage_items(store: Store, w: Window, phase: int) -> list[Criterion]:
    rates = coverage(store, w, ("merge",))
    out = []
    for tier, target in COVERAGE_TARGET[phase - 1].items():
        r = rates.get(tier, Rate(0, 0))
        v = r.value
        out.append(_crit(f"coverage tier {tier} at least {100 * target:.0f} percent (merge runs)",
                         None if v is None else v >= target, r.text()))
    return out


def _metric_items(store: Store, w: Window, m: Metrics, phase: int) -> list[Criterion]:
    i = phase - 1
    return ([_below(f"false-positive rate under {100 * FP_TARGET[i]:.0f} percent", m.false_positive.rate, FP_TARGET[i]),
             _below(f"flake rate under {100 * FLAKE_TARGET[i]:.0f} percent", m.flake, FLAKE_TARGET[i])]
            + _triage_items(m, phase) + _coverage_items(store, w, phase)
            + [_below("aborted_harness plus aborted_budget under "
                      f"{100 * ABORT_TARGET[i]:.0f} percent of runs", m.harness.aborted.get("all", Rate(0, 0)),
                      ABORT_TARGET[i]),
               _below(f"env_error under {100 * ENV_ERROR_TARGET[i]:.0f} percent of checkpoints", m.harness.env_error,
                      ENV_ERROR_TARGET[i])])


def _reviewer_gates(m: Metrics) -> list[Criterion]:
    newest = max(m.reviewer.evals, key=lambda e: e.run_at, default=None)
    out = []
    for name, limit in EVAL_GATES:
        v = eval_value(newest.metrics, name) if newest else None
        where = f" ({newest.model_id}, prompt {newest.prompt_hash})" if newest else ""
        out.append(_crit(f"Reviewer {name.replace('_', ' ')} at least {limit}", None if v is None else v >= limit,
                         ("no eval in the window" if newest is None else f"{v}{where}")))
    return out


def _pilot(store: Store, end: datetime) -> list[Criterion]:
    rels = _release_windows(store, end)
    catch = store.query("SELECT COUNT(*) FROM findings WHERE kind = 'collateral' AND synthetic = 0 AND triage_state = "
                        "'confirmed_bug'")[0][0]
    out = [_crit("two release cycles", len(rels) >= 2, f"{len(rels)} releases"),
           _crit("at least one real pre-release catch on an undeclared registered surface", catch > 0,
                 f"{catch} collateral findings confirmed as bugs")]
    if not rels:
        out.append(_crit("canaries at target every night of the last cycle", None, "no release yet"))
        return out
    c = canary(store, Window(rels[-1][1], rels[-1][2]))
    bad = [run for run, money, other in c.nights if money.num != money.den or
           (other.value is not None and other.value < CANARY_OTHER_TARGET)]
    out.append(_crit("canaries at target every night of the last cycle", None if not c.nights else not bad,
                     f"below target on {', '.join(bad)}" if bad else f"{len(c.nights)} nights at target"))
    return out


def _fallback_per_flow(store: Store, w: Window) -> Criterion:
    item = "fallback rate per flow under 5 percent"
    clause, args = w.runs()
    rows = store.query(f"SELECT e.flow_id, COUNT(*) AS n, SUM(e.step_outcome = 'passed_via_agent_fallback') AS fb FROM "
                       f"step_events e JOIN runs r ON r.run_id = e.run_id WHERE e.flow_id IS NOT NULL AND "
                       f"e.step_outcome IS NOT NULL AND e.drill = 0 AND {clause} GROUP BY e.flow_id", args)
    if not rows:
        return _crit(item, None, "no flow step in the window")
    over = [f"{r['flow_id']} {100 * r['fb'] / r['n']:.0f}%" for r in rows if r["fb"] / r["n"] >= FALLBACK_TARGET]
    return _crit(item, not over, ", ".join(over) if over else f"{len(rows)} flows under 5 percent")


def _rc_inside_budget(store: Store, w: Window) -> Criterion:
    clause, args = w.runs()
    rows = store.query(f"SELECT r.run_id, r.started_at, r.finished_at FROM runs r WHERE {clause} AND r.run_type = 'rc' "
                       "AND r.outcome = 'complete'", args)
    inside = [r["run_id"] for r in rows
              if (_t(r["finished_at"]) - _t(r["started_at"])).total_seconds() / 60 <= RUN_BUDGET_MIN["rc"]]
    return _crit("one RC run completed inside its 8-hour budget", bool(inside) if rows else None,
                 f"inside: {', '.join(inside)}" if inside else f"{len(rows)} complete RC runs, none inside 8 hours")


NOT_IN_STORE = {
    2: ("every tier-1 surface has a flow with assertions: needs the registry and flows of the config commit",
        "no human-disposed diff reappears undisposed: needs the signature history audit",
        "the size sweep reproduces the ladder overlap as invariant_violation: needs the acceptance evidence"),
    3: ("RC run triaged and signed off before GA with the lineage baseline approved and the G-1 migration checkpoints "
        "appended: sign-off is recorded outside the store",
        "the Explorer stays inside its budgets with zero deny-list violations: needs the Explorer session ledger",
        "at least 80 percent of flows authored or repaired through the Recorder in the last four weeks: needs git "
        "history", "the kill criteria evaluated after two release cycles and recorded in the metrics history: needs "
        "the recorded evaluation"),
}


def phase_exit(store: Store, phase: int, *, now: datetime | None = None,
               acceptance: Sequence[AcceptanceResult] = ()) -> PhaseExit:
    """Each 11.9 exit item of `phase` (1, 2 or 3) over the 14 days ending `now` (choice 10)."""
    if phase not in (1, 2, 3):
        raise ValueError(f"phase {phase}; Phases 1 to 3 have exits (11.9)")
    end = _now(now)
    w = Window(end - timedelta(days=14), end, label="last two weeks")
    out: list[Criterion] = []
    if phase == 1:
        m = compute(store, w)
        out += [_acceptance(acceptance), _fingerprints(store, w), _floors(store), _budgets(store, end),
                _canaries(store, w, 1), _drill(store, end), _outcomes_complete(store, w)]
        out += _metric_items(store, w, m, 1)
    elif phase == 2:
        m = compute(store, w)
        out += _metric_items(store, w, m, 2) + [_canaries(store, w, 2)] + _reviewer_gates(m) + _pilot(store, end)
    else:
        share = tier1_quarantine_share(store)
        out += [_rc_inside_budget(store, Window(end - timedelta(days=42), end)),
                _crit("quarantine under 2 percent of tier-1 checkpoints", not share.red,
                      f"{share.quarantined} of {share.checkpoints} tier-1 checkpoints quarantined"),
                _fallback_per_flow(store, Window(end - timedelta(days=28), end))]
    out += [Criterion(text.split(":")[0], NOT_MEASURABLE, text.split(": ", 1)[1])
            for text in NOT_IN_STORE.get(phase, ())]
    return PhaseExit(phase, tuple(out))
