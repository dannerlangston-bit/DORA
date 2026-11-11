"""The RC soak (10.11, 1.9): budgets, their evaluation per cycle, the freeze detector, log signatures, the lineage
comparison rule and the RC report's soak section.

One rig (rig 5), the candidate build, the tier B workspace with the most gadgets (`soak_workspace`), 24 cycles of
15 minutes, each ending with the checkpoint `flow/<workspace-surface-id>/soak/c<nn>` (scope.py plans them). The
resource sampler (every 60 s, every process of the tree: working set, private bytes, GDI and USER objects, handles,
threads, CPU, heap after GC) and the 2 s clock-region hash are the Windows track's; this module evaluates what they
recorded (`Sample`, `ClockSample`, the 8.3 `soak_samples` rows through `samples_from_rows`).

Budgets (`SoakBudgets`, from `soak.yaml`), each the assertion `soak.<metric>` evaluated at every cycle checkpoint
(`evaluate`): working set growth under 25 percent above the hour-1 value and under 1.5 GB; the slope of a linear fit
over the trailing 3 hours of working set or private bytes under 60 MB per hour; GDI objects under 9,000 at every
sample; handles and threads within 20 percent of the hour-1 value; heap after GC under 80 percent of `-Xmx` where
readable; zero freezes and relaunches; a clock tick in at least 99 percent of the 2 s windows. A breach is
`assertion_failed (assertion: soak.<metric>)` on the cycle checkpoint, tier 1 (`cycle_outcome`).

The freeze detector (`freeze_detector`): the clock region hash unchanged for 10 s with no anti-anchor on screen is a
freeze, the app-failure trio with cause `hang` (`freeze_result`, through `watchdog.app_failure`); the soak continues
after the relaunch under the same 3-relaunch budget. New exception signatures in the client log slice
(`log_signatures`) are tier-2 `new_log_signature` findings with the count and the first occurrence, never outcomes;
a log that cannot be located prints "log signatures: not available". The lineage soak is comparable only with a
matching fingerprint (`rc_lineage.lineage_soak`); otherwise the lineage comparison is `needs_baseline` and the
candidate curves are shown alone. Cycles the soak never reached are `not_run (reason: budget)` (`unreached`) and are
printed as such in the release gate checklist (`gate_line`).

Choices (reported):
1. Working set, private bytes, handles and threads are the tree's totals at each sample time; GDI objects are judged
   per process (the 10,000 quota is per process); USER objects and CPU are recorded and drawn, with no budget.
2. The hour-1 value is the median of the tree totals sampled in the first 60 minutes; the relative budgets
   (working set growth, handles, threads) are evaluated on samples after the first hour, so cycles 1 to 4 can breach
   only the absolute ones. "Within 20 percent" holds in both directions.
3. The slope is fitted at each cycle end from the first cycle whose end is at least the window (3 h) after the
   start, over the samples of the trailing window; MB is 2^20 bytes (1.5 GB = 1536 MB, as soak.yaml writes it).
4. A budget is breached at its bound ("under 9,000" fails at 9,000); render latency compares the share of 2 s clock
   windows of the cycle in which the hash changed with the minimum.
5. A log signature is the normalized first three frames of a stack (line numbers dropped, anonymous class and lambda
   numbering replaced), as 10.11 defines it; the exception class does not take part. Without a lineage signature set
   no signature is new (the section says so) rather than every signature.
6. A cycle's outcome from this module, highest first in the 1.10 order: the app failure on the cycle (`app_alive`),
   the first breached metric in `METRICS` order with every breached id in `assertion_ids`, then `needs_baseline
   (reason: lineage_missing)` when the lineage soak is not comparable; None leaves the cycle to the normal diff
   path (against the lineage capture and the run's cycle-1 capture).
7. A missing `soak.yaml` gives the 10.11 defaults; `looser_budgets` names every budget looser than its default, for
   the approval check of L-XREF-27.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .orchestrator.budgets import ShardBudgets
from .orchestrator.results import ControlResult, FindingSpec, Outcome
from .orchestrator.rig import CrashSignal
from .orchestrator.watchdog import app_failure

METRICS = ("working_set", "growth_slope", "gdi_objects", "handles", "threads", "heap", "freezes", "relaunches",
           "render_latency")                                   # the soak.<metric> assertions of 10.11, in table order
SAMPLE_METRICS = ("working_set", "private_bytes", "gdi_objects", "user_objects", "handles", "threads", "cpu",
                  "heap_after_gc", "clock_tick")              # 8.3 soak_samples.metric
TREE_METRICS = ("working_set", "private_bytes", "handles", "threads", "user_objects", "cpu")   # choice 1
CYCLES = 24
CYCLE_S = 900.0                                                # one cycle every 15 minutes (10.11)
HOUR_S = 3600.0
FREEZE_S = 10.0                                                # 10.11: unchanged 10 s with no anti-anchor
MB = 1024 * 1024
SOAK_FILE = "soak.yaml"
NOT_AVAILABLE = "log signatures: not available"
SOAK_TIER = 1
LOG_FINDING_TIER = 2


def assertion(metric: str) -> str:
    """The assertion id of a soak metric (`soak.working_set`, ...)."""
    if metric not in METRICS:
        raise ValueError(f"soak metric {metric!r}; one of {METRICS}")
    return f"soak.{metric}"


def cycle_checkpoint(surface_id: str, cycle: int) -> str:
    """`flow/<workspace-surface-id>/soak/c<nn>` (1.9, scope.py)."""
    return f"flow/{surface_id}/soak/c{cycle:02d}"


# -- budgets ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SoakBudgets:
    """`soak.yaml` (4.16 soak schema); the defaults are the 10.11 table."""

    working_set_growth_pct_max: float = 25.0
    working_set_growth_mb_max: float = 1536.0
    slope_window_h: float = 3.0
    slope_mb_per_h_max: float = 60.0
    gdi_objects_max: int = 9000
    handles_within_pct: float = 20.0
    threads_within_pct: float = 20.0
    heap_pct_of_xmx_max: float = 80.0
    freezes_max: int = 0
    relaunches_max: int = 0
    tick_window_s: int = 2
    tick_windows_pct_min: float = 99.0
    calibrated_from: str | None = None

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any] | None) -> "SoakBudgets":
        """From the parsed `soak.yaml`; a missing file or key keeps the 10.11 default (choice 7)."""
        if not doc:
            return cls()
        b = doc.get("budgets") or {}
        d = cls()

        def get(metric: str, key: str, default: Any) -> Any:
            v = (b.get(metric) or {}).get(key)
            return default if v is None else v

        return cls(get("working_set", "growth_pct_max", d.working_set_growth_pct_max),
                   get("working_set", "growth_mb_max", d.working_set_growth_mb_max),
                   get("growth_slope", "window_h", d.slope_window_h),
                   get("growth_slope", "mb_per_h_max", d.slope_mb_per_h_max),
                   get("gdi_objects", "max", d.gdi_objects_max), get("handles", "within_pct", d.handles_within_pct),
                   get("threads", "within_pct", d.threads_within_pct),
                   get("heap", "pct_of_xmx_max", d.heap_pct_of_xmx_max), get("freezes", "max", d.freezes_max),
                   get("relaunches", "max", d.relaunches_max), get("render_latency", "tick_window_s", d.tick_window_s),
                   get("render_latency", "windows_pct_min", d.tick_windows_pct_min), doc.get("calibrated_from"))

    def lines(self) -> dict[str, str]:
        """The budget line per metric, as the report draws it."""
        return {"working_set": f"< {self.working_set_growth_pct_max:g}% and < {self.working_set_growth_mb_max:g} MB "
                               "above hour 1",
                "growth_slope": f"< {self.slope_mb_per_h_max:g} MB/h over {self.slope_window_h:g} h",
                "gdi_objects": f"< {self.gdi_objects_max}", "handles": f"within {self.handles_within_pct:g}% of hour 1",
                "threads": f"within {self.threads_within_pct:g}% of hour 1",
                "heap": f"< {self.heap_pct_of_xmx_max:g}% of -Xmx", "freezes": f"<= {self.freezes_max}",
                "relaunches": f"<= {self.relaunches_max}",
                "render_latency": f">= {self.tick_windows_pct_min:g}% of {self.tick_window_s} s windows tick"}


def load_budgets(config_root: str | Path) -> SoakBudgets:
    """The budgets of a sentinel-config checkout's `soak.yaml` (defaults when absent)."""
    p = Path(config_root) / SOAK_FILE
    return SoakBudgets.from_doc(yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else None)


# (metric, key, default, direction): +1 when a larger value is looser, -1 when a smaller one is
_LOOSENESS = (("working_set", "growth_pct_max", 25, 1), ("working_set", "growth_mb_max", 1536, 1),
              ("growth_slope", "window_h", 3, 1), ("growth_slope", "mb_per_h_max", 60, 1),
              ("gdi_objects", "max", 9000, 1), ("handles", "within_pct", 20, 1), ("threads", "within_pct", 20, 1),
              ("heap", "pct_of_xmx_max", 80, 1), ("freezes", "max", 0, 1), ("relaunches", "max", 0, 1),
              ("render_latency", "tick_window_s", 2, 1), ("render_latency", "windows_pct_min", 99, -1))


def looser_budgets(doc: Any) -> list[tuple[str, str]]:
    """Every budget of a parsed `soak.yaml` looser than its 10.11 default, as (path, message) (L-XREF-27; a longer
    slope window dilutes a recent leak, a longer tick window hides stalls)."""
    budgets = doc.get("budgets") if isinstance(doc, Mapping) else None
    out: list[tuple[str, str]] = []
    for metric, key, default, sign in _LOOSENESS:
        v = budgets.get(metric, {}).get(key) if isinstance(budgets, Mapping) and isinstance(budgets.get(metric),
                                                                                             Mapping) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and (v - default) * sign > 0:
            out.append((f"budgets.{metric}.{key}", f"soak.{metric} {key} {v:g} is looser than the 10.11 default "
                                                   f"{default:g}"))
    return out


# -- samples ---------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Sample:
    """One sampler reading: seconds since the soak started, the process, the metric and the value."""

    t_s: float
    pid: int
    metric: str
    value: float

    def __post_init__(self) -> None:
        if self.metric not in SAMPLE_METRICS:
            raise ValueError(f"soak sample metric {self.metric!r}; one of {SAMPLE_METRICS}")


@dataclass(frozen=True)
class ClockSample:
    """One 2 s hash of the ticking clock region, and whether an anti-anchor was on screen."""

    t_s: float
    sha256: str
    anti_anchor: bool = False


def samples_from_rows(rows: Iterable[Mapping[str, Any]], start: datetime) -> list[Sample]:
    """8.3 `soak_samples` rows (sampled_at, pid, metric, value) as `Sample`s relative to the soak start."""
    out = []
    for r in rows:
        at = r["sampled_at"]
        t = datetime.fromisoformat(at.replace("Z", "+00:00")) if isinstance(at, str) else at
        out.append(Sample((t - start).total_seconds(), int(r["pid"]), str(r["metric"]), float(r["value"])))
    return sorted(out, key=lambda s: (s.t_s, s.pid, s.metric))


def tree_series(samples: Iterable[Sample], metric: str) -> list[tuple[float, float]]:
    """The tree total of a metric at each sample time, in time order (choice 1)."""
    acc: dict[float, float] = defaultdict(float)
    for s in samples:
        if s.metric == metric:
            acc[s.t_s] += s.value
    return sorted(acc.items())


def _hour1(series: Sequence[tuple[float, float]]) -> float | None:
    vals = [v for t, v in series if t <= HOUR_S]
    return statistics.median(vals) if vals else None


def slope_mb_per_h(series: Sequence[tuple[float, float]]) -> float | None:
    """The least-squares slope of a (seconds, bytes) series in MB per hour; None with fewer than two times."""
    if len({t for t, _ in series}) < 2:
        return None
    t = np.array([p[0] for p in series], dtype=float)
    v = np.array([p[1] for p in series], dtype=float)
    return float(np.polyfit(t, v, 1)[0]) * HOUR_S / MB


# -- the freeze detector ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Freeze:
    """A clock region whose hash stayed the same from `start_s` until detection at `detected_s` (10.11)."""

    start_s: float
    detected_s: float


def freeze_detector(samples: Sequence[ClockSample], freeze_s: float = FREEZE_S) -> list[Freeze]:
    """The freezes in a clock-hash series: the same hash for `freeze_s` seconds with no anti-anchor on screen; one
    per frozen stretch, detected the moment it reaches `freeze_s` (10.11)."""
    out: list[Freeze] = []
    start: ClockSample | None = None
    flagged = False
    for s in sorted(samples, key=lambda x: x.t_s):
        if s.anti_anchor:
            start, flagged = None, False
            continue
        if start is None or s.sha256 != start.sha256:
            start, flagged = s, False
            continue
        if not flagged and s.t_s - start.t_s >= freeze_s:
            out.append(Freeze(start.t_s, s.t_s))
            flagged = True
    return out


def freeze_result(budgets: ShardBudgets) -> ControlResult:
    """A freeze is the app-failure trio with cause `hang` (10.11, 10.1): the cycle in progress `assertion_failed
    (assertion: app_alive)`, the `app_crash` finding, a relaunch from the shard's 3, or `aborted_budget
    (reason: app_restart_budget)` at the fourth."""
    return app_failure(CrashSignal("hang", "screenshot_staleness"), budgets, phase="running")


def tick_share(samples: Sequence[ClockSample]) -> float | None:
    """The percentage of 2 s windows in which the clock hash changed from the previous window; None without one."""
    s = sorted(samples, key=lambda x: x.t_s)
    if len(s) < 2:
        return None
    ticks = sum(1 for a, b in zip(s, s[1:]) if a.sha256 != b.sha256)
    return 100.0 * ticks / (len(s) - 1)


# -- evaluation ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Breach:
    """One budget breach on a cycle: the metric, when, the value measured and the bound."""

    metric: str
    t_s: float
    value: float
    limit: float
    detail: str

    @property
    def assertion(self) -> str:
        return assertion(self.metric)


@dataclass(frozen=True)
class CycleVerdict:
    """The breaches of one cycle, in `METRICS` order (one per metric, the first sample that breached)."""

    cycle: int
    breaches: tuple[Breach, ...] = ()

    @property
    def assertion_ids(self) -> tuple[str, ...]:
        return tuple(b.assertion for b in self.breaches)


def _window(cycle: int, cycle_s: float) -> tuple[float, float]:
    return (cycle - 1) * cycle_s, cycle * cycle_s


def evaluate(samples: Sequence[Sample], budgets: SoakBudgets = SoakBudgets(), *, cycles: int = CYCLES,
             cycle_s: float = CYCLE_S, xmx_bytes: float | None = None, freezes: Sequence[float] = (),
             relaunches: Sequence[float] = (), clock: Sequence[ClockSample] = ()) -> list[CycleVerdict]:
    """The budget verdict of cycles 1 to `cycles` (10.11; choices 1 to 4). `freezes` and `relaunches` are the
    seconds at which each happened; `clock` the 2 s clock hashes."""
    b = budgets
    ws, pb = tree_series(samples, "working_set"), tree_series(samples, "private_bytes")
    hd, th = tree_series(samples, "handles"), tree_series(samples, "threads")
    h1 = {"working_set": _hour1(ws), "handles": _hour1(hd), "threads": _hour1(th)}
    gdi = sorted((s.t_s, s.pid, s.value) for s in samples if s.metric == "gdi_objects")
    heap = sorted((s.t_s, s.value) for s in samples if s.metric == "heap_after_gc")
    win_s = b.slope_window_h * HOUR_S
    out: list[CycleVerdict] = []
    for n in range(1, cycles + 1):
        lo, hi = _window(n, cycle_s)
        inside = (lambda t: lo < t <= hi)
        found: dict[str, Breach] = {}

        def note(metric: str, t: float, value: float, limit: float, detail: str) -> None:
            found.setdefault(metric, Breach(metric, t, value, limit, detail))

        base = h1["working_set"]
        for t, v in ws:
            if inside(t) and t > HOUR_S and base:
                growth = v - base
                if growth >= base * b.working_set_growth_pct_max / 100:
                    note("working_set", t, v, base * (1 + b.working_set_growth_pct_max / 100),
                         f"working set {v / MB:.0f} MB is {100 * growth / base:.0f}% above hour 1 ({base / MB:.0f} MB)")
                elif growth >= b.working_set_growth_mb_max * MB:
                    note("working_set", t, v, base + b.working_set_growth_mb_max * MB,
                         f"working set grew {growth / MB:.0f} MB above hour 1")
        if hi >= win_s:
            for name, series in (("working set", ws), ("private bytes", pb)):
                slope = slope_mb_per_h([(t, v) for t, v in series if hi - win_s <= t <= hi])
                if slope is not None and slope >= b.slope_mb_per_h_max:
                    note("growth_slope", hi, slope, b.slope_mb_per_h_max,
                         f"{name} grows {slope:.1f} MB/h over the trailing {b.slope_window_h:g} h")
        for t, pid, v in gdi:
            if inside(t) and v >= b.gdi_objects_max:
                note("gdi_objects", t, v, b.gdi_objects_max, f"process {pid} holds {v:.0f} GDI objects")
        for metric, series, pct in (("handles", hd, b.handles_within_pct), ("threads", th, b.threads_within_pct)):
            ref = h1[metric]
            for t, v in series:
                if inside(t) and t > HOUR_S and ref and abs(v - ref) > ref * pct / 100:
                    note(metric, t, v, ref, f"{metric} {v:.0f} is {100 * (v - ref) / ref:+.0f}% from hour 1 "
                                            f"({ref:.0f})")
        if xmx_bytes:
            for t, v in heap:
                if inside(t) and v >= xmx_bytes * b.heap_pct_of_xmx_max / 100:
                    note("heap", t, v, xmx_bytes * b.heap_pct_of_xmx_max / 100,
                         f"heap after GC {100 * v / xmx_bytes:.0f}% of -Xmx")
        for metric, times, limit in (("freezes", freezes, b.freezes_max), ("relaunches", relaunches,
                                                                          b.relaunches_max)):
            total = sum(1 for t in times if t <= hi)
            mine = [t for t in times if inside(t)]
            if mine and total > limit:
                note(metric, mine[0], total, limit, f"{total} {metric} so far (budget {limit})")
        share = tick_share([c for c in clock if inside(c.t_s)])
        if share is not None and share < b.tick_windows_pct_min:
            note("render_latency", hi, share, b.tick_windows_pct_min,
                 f"the clock ticked in {share:.1f}% of {b.tick_window_s} s windows")
        out.append(CycleVerdict(n, tuple(found[m] for m in METRICS if m in found)))
    return out


def cycle_outcome(verdict: CycleVerdict, *, app_failed: Outcome | None = None, lineage_comparable: bool = True
                  ) -> Outcome | None:
    """The outcome this module decides for a cycle checkpoint (choice 6), or None for the normal diff path."""
    if app_failed is not None:
        return app_failed
    if verdict.breaches:
        first = verdict.breaches[0]
        return Outcome("assertion_failed", f"assertion:{first.assertion}",
                       detail="; ".join(f"{x.assertion}: {x.detail}" for x in verdict.breaches))
    if not lineage_comparable:
        return Outcome("needs_baseline", "lineage_missing",
                       detail="the lineage soak's fingerprint differs from the current one (10.11)")
    return None


def unreached(surface_id: str, reached: int, cycles: int = CYCLES) -> dict[str, Outcome]:
    """`not_run (reason: budget)` for every cycle the soak did not reach (10.11)."""
    return {cycle_checkpoint(surface_id, n): Outcome("not_run", "budget", detail="the soak did not reach this cycle")
            for n in range(reached + 1, cycles + 1)}


def gate_line(outcomes: Mapping[str, Outcome | None], cycles: int = CYCLES) -> str:
    """The soak line of the release gate checklist: never "passed" while a cycle is unreached (10.11)."""
    missing = sum(1 for o in outcomes.values() if o is not None and o.outcome == "not_run")
    failed = sum(1 for o in outcomes.values() if o is not None and o.outcome == "assertion_failed")
    if missing:
        return f"soak: not finished, {missing} of {cycles} cycles not_run (budget)"
    if failed:
        return f"soak: {failed} of {cycles} cycles breached a budget or failed app_alive"
    return f"soak: {cycles} cycles run"


# -- log signatures ----------------------------------------------------------------------------------------------
_FRAME = re.compile(r"^\s*at\s+([\w$.<>/\-]+)\s*\(")
_HEADER = re.compile(r"^(?:Exception in thread \"[^\"]*\"\s+)?(?:Caused by:\s+)?"
                     r"((?:[a-zA-Z_][\w$]*\.)+[\w$]*(?:Exception|Error|Throwable))\b")
_LAMBDA = re.compile(r"\$\$Lambda(?:\$\d+)?(?:/0x[0-9a-f]+)?(?:/\d+)?")
_ANON = re.compile(r"\$\d+")


def normalize_frame(frame: str) -> str:
    """One stack frame as a signature part: the method only (no file or line), with lambda and anonymous class
    numbering replaced (choice 5)."""
    return _ANON.sub("$N", _LAMBDA.sub("$$Lambda", frame))


def stack_signatures(log_text: str) -> list[tuple[str, int, str]]:
    """Every stack in a log slice: (signature, line number of its header, the header line)."""
    lines = log_text.splitlines()
    out: list[tuple[str, int, str]] = []
    i = 0
    while i < len(lines):
        if _HEADER.match(lines[i].strip()):
            frames: list[str] = []
            j = i + 1
            while j < len(lines) and (m := _FRAME.match(lines[j])):
                if len(frames) < 3:
                    frames.append(normalize_frame(m.group(1)))
                j += 1
            if frames:
                out.append((" | ".join(frames), i + 1, lines[i].strip()))
            i = j
            continue
        i += 1
    return out


@dataclass(frozen=True)
class LogSignatures:
    """The log slice's signatures: whether the log was located, each signature's count and first occurrence, the
    lineage set's availability and the signatures absent from it."""

    available: bool
    counts: Mapping[str, int] = field(default_factory=dict)
    first: Mapping[str, str] = field(default_factory=dict)
    lineage_available: bool = True
    new: tuple[str, ...] = ()

    @property
    def line(self) -> str:
        """The soak section's line (10.11: "not available" rather than zero)."""
        if not self.available:
            return NOT_AVAILABLE
        if not self.lineage_available:
            return f"log signatures: {len(self.counts)} seen, no lineage set to compare"
        return f"log signatures: {len(self.new)} new of {len(self.counts)} seen"

    def findings(self) -> list[FindingSpec]:
        """One tier-2 `new_log_signature` finding per new signature, with the count and the first occurrence."""
        return [FindingSpec("new_log_signature", f"new exception signature: {sig}", tier=LOG_FINDING_TIER,
                            detail=f"count {self.counts[sig]}; first: {self.first[sig]}") for sig in self.new]


def log_signatures(log_text: str | None, lineage: Iterable[str] | None) -> LogSignatures:
    """The signatures of a client log slice against the lineage run's set (10.11; choice 5). `log_text` None: the log
    could not be located (10.2)."""
    if log_text is None:
        return LogSignatures(False)
    counts: dict[str, int] = {}
    first: dict[str, str] = {}
    for sig, line_no, header in stack_signatures(log_text):
        counts[sig] = counts.get(sig, 0) + 1
        first.setdefault(sig, f"line {line_no}: {header}")
    if lineage is None:
        return LogSignatures(True, counts, first, lineage_available=False)
    known = set(lineage)
    return LogSignatures(True, counts, first, True, tuple(s for s in counts if s not in known))


# -- the soak workspace and the report section -----------------------------------------------------------------------
def soak_workspace(workspaces: Iterable[Mapping[str, Any]]) -> str | None:
    """The tier B workspace with the most gadgets (1.9, 10.11); ties go to the lower id; None without tier B."""
    tier_b = [w for w in workspaces if w.get("tier") == "B" and isinstance(w.get("id"), str)]
    if not tier_b:
        return None
    return min(tier_b, key=lambda w: (-len(w.get("gadgets_present") or ()), w["id"]))["id"]


def curves(samples: Sequence[Sample]) -> dict[str, list[tuple[float, float]]]:
    """Per metric, the series the report draws: tree totals, and the per-sample maximum for GDI objects and heap."""
    out = {m: tree_series(samples, m) for m in TREE_METRICS}
    for m in ("gdi_objects", "heap_after_gc"):
        acc: dict[float, float] = {}
        for s in samples:
            if s.metric == m:
                acc[s.t_s] = max(acc.get(s.t_s, 0.0), s.value)
        out[m] = sorted(acc.items())
    return {m: v for m, v in out.items() if v}


def soak_section(samples: Sequence[Sample], budgets: SoakBudgets, outcomes: Mapping[str, Outcome | None], *,
                 lineage: Sequence[Sample] | None, lineage_comparable: bool, logs: LogSignatures,
                 relaunches: int, freezes: int, xmx_bytes: float | None = None) -> dict[str, Any]:
    """The RC report's soak section data (9.4, 10.11): both builds' curves per metric with the budget lines (the
    candidate's alone when the lineage is not comparable), restarts, freezes, new log signatures, the memory ceiling
    reached and the 24 cycle outcomes."""
    ws = tree_series(samples, "working_set")
    heap = [s.value for s in samples if s.metric == "heap_after_gc"]
    return {
        "candidate_curves": curves(samples),
        "lineage_curves": curves(lineage) if lineage is not None and lineage_comparable else None,
        "lineage_note": None if lineage_comparable else "lineage soak not comparable (fingerprint differs): "
                                                        "candidate curves shown alone",
        "budget_lines": budgets.lines(),
        "relaunches": relaunches, "freezes": freezes,
        "new_log_signatures": [{"signature": s, "count": logs.counts[s], "first": logs.first[s]} for s in logs.new],
        "log_signatures_line": logs.line, "log_signatures": sorted(logs.counts),
        "memory_ceiling_mb": round(max(v for _, v in ws) / MB, 1) if ws else None,
        "heap_ceiling_pct": round(100 * max(heap) / xmx_bytes, 1) if heap and xmx_bytes else None,
        "cycles": {cid: (o.outcome if o else None, o.reason if o else None) for cid, o in sorted(outcomes.items())},
        "gate_line": gate_line(outcomes),
    }
