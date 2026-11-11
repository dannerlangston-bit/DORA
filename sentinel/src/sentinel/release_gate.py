"""The release gate: `policy/release_gate.yaml`, the RC deadlines and the G-N sign-off conditions (4.16, 10.9, 11.1
PA-14, 11.2 RC row, 11.5, 11.6, 11.7 row 12, 9.8, 9.12, 10.3, 10.5, 10.11; SD-93; lint rule L-XREF-23).

What it provides, and who calls it:

- `load(path)` reads `policy/release_gate.yaml` (4.16 schema `release-gate`) into a `ReleaseGatePolicy`; a missing
  file is the policy with `present=False`, under which N = 3 and an RC cut at G-7 are assumed (4.16, 10.9, 11.7 row
  12). `from_doc(doc)` reads a loaded document; `config_problems(doc)` is lint rule L-XREF-23
  (`lint/checks/release_gate.py`).
- `ReleaseGatePolicy.rc_deadlines(train, cut_at=...)` (also `rc_deadlines(policy, train, ...)`) resolves a train at
  RC cut: N, the GA and RC cut dates, the G-N sign-off time and the RC start deadline, the sign-off time minus the
  8 hours of the RC budget (11.2, `start_deadline.RC_SIGNOFF_LEAD`), plus the assumption line the RC report header
  prints. `record_deadlines` keeps them on the RC run (`runs.summary_json["release_gate"]`), so the run's clocks stay
  what the file said at RC cut; `stored_deadlines` reads them back. `RcDeadlines.signoff_at` is the `signoff_at` of
  the queue's `RcBuild` and of `start_deadline.deadline("rc", ...)`, and the `rc_deadlines` mapping of
  `sla.open_items` / `sla.rc_signoff_refusals`.
- `evaluate(store, rc_run_id, now=...)` returns every G-N condition of 10.9 with pass or fail and its evidence
  (`GateResult`): the RC report's release gate checklist and the refusals of `report.rc.sign_off`.
- `rc_gate(...)` is the four-input RC gate of 1.5, 9.11 and 11.5 (quarantine share, detector state, tier-1 coverage,
  tier-1 `needs_baseline`), for the run-health line of a merge or Nightly run before any RC run exists ("Tier 1
  below 100 percent in a merge run blocks that release's RC gate", 9.11).

The G-N conditions (`CONDITIONS`, in checklist order; each key is also the refusal name of `report.rc.sign_off`):

- `run`: the run is an RC run that has finished, and it is not `aborted_budget (reason: superseded)` (9.4: a
  superseded RC run cannot be signed).
- `detector`: `system_state.detector_state` is `ok` and was written by the last finished Nightly run (no
  `detector_blind`, `detector_degraded` or `generation_failed`); that Nightly has no `harness_error (reason:
  canary_path_divergence)` row (SD-93); and no "canaries pending" banner is up (11.6: "no RC sign-off until
  canaries pass").
- `coverage`: every tier-1 cell of the RC run's `coverage_cells` is covered or its `not_run` is explained.
- `quarantine`: tier-1 checkpoints `not_run (reason: quarantine)` are at most 2 percent of the RC run's tier-1
  checkpoints (`coverage.QUARANTINE_CEILING`; 10.5, 9.11).
- `dispositions`: no tier-1 `collateral`, `invariant_violation`, `assertion_failed` or `needs_human` without a
  disposition, and nothing in `sla.rc_signoff_refusals` (an untriaged tier-1 item of the RC run, or any item five
  business days past its SLA, 9.12 step 4).
- `baselines`: no tier-1 checkpoint with a pending candidate on the lineage, and no tier-1 `needs_baseline` of the RC
  run without an approved baseline on the lineage (9.8, 11.5).
- `soak`: every soak cycle checkpoint (`flow/<surface>/soak/c<nn>`) ran, and every soak finding and breach
  (`assertion_failed (assertion: soak.<metric>)`) is dispositioned (10.11).
- `fire_drill`: the last finished Nightly run recorded fire drill items, all blocked: no `drill_failed`, no
  `drill_order_placed` (6.5, 10.9).
- `proof`: every shard of the RC run has a post-run proof with a PROVEN verdict (6.6, 10.9).

Choices where the spec is ambiguous (each is reported):

1. The sign-off time is 17:00 America/New_York (the close of `sla.BusinessClock`) on G-N, the Nth business day
   before GA counted on the clock's business days (XNYS less firm holidays), GA itself not counted. So the RC start
   deadline, the sign-off time minus 8 hours, is 09:00 on G-N. A GA on Thursday with N = 3 signs off on Monday.
2. "An RC cut at G-7" counts 7 business days, the unit of G-N (10.9) and of L-XREF-23. With no file, or a train the
   file does not list, GA is placed 7 business days after the RC cut date (`cut_at`, the RC run's trigger); N is 3
   with no file and the file's `default_business_days` for an unlisted train. Each assumption is named in
   `RcDeadlines.assumed` and the header line. A listed train with no `cut_at` uses its `rc_cut_date`.
3. A `not_run` tier-1 cell is explained when its reason is `entitlement` or `scope`, the reasons that leave the
   coverage denominator (2.12, 9.11, `coverage.DENOMINATOR_EXCLUDED`); every other `not_run` reason, `blocked:<outcome>`
   included, fails `coverage`. An RC run with no `coverage_cells` rows fails it ("no coverage matrix").
4. The quarantine share is counted on the RC run's rows as `coverage` choice 5 counts it: distinct tier-1
   checkpoints `not_run (reason: quarantine)` over the tier-1 checkpoints of the run (scope and outcomes) less those
   `not_run (entitlement)` or `not_run (scope)`; strictly above 2 percent blocks.
5. A disposition is an inherited decision on the outcome row, or a finding of the run on the checkpoint that is no
   longer `open` or carries a decision. An outcome with no finding at all and no decision is undispositioned; an
   open finding is reported once, through `sla.rc_signoff_refusals`.
6. PROVEN means the verdicts `proven_no_sends`, `proven_declared_sends` and `proven_no_session`. An UNPROVEN proof
   cleared by hand (`verified_by`) is still not PROVEN and fails `proof`; a shard in state `lost` or `superseded`
   (re-queued, 10.7) needs no proof of its own. A run with no shard needs its run-level row (SD-61).
7. "The last Nightly run" is the latest finished Nightly by `started_at`; `fire_drill` and `detector` read it.
8. A respin keeps its train's clock (10.9: "the clock does not reset"): the new RC run records the superseded or
   earlier run's deadlines, `record_deadlines(store, new_run, stored_deadlines(store, old_run))`, rather than
   resolving an assumed G-7 from its own cut. A listed train resolves to the same times either way.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from . import calendar as cal
from .canary import read_detector_state
from .coverage import DENOMINATOR_EXCLUDED, QUARANTINE_CEILING, QuarantineShare
from .orchestrator.start_deadline import RC_SIGNOFF_LEAD
from .report.safety import drill_summary
from .sla import BusinessClock, rc_signoff_refusals
from .store import Store

POLICY_PATH = "policy/release_gate.yaml"
DEFAULT_BUSINESS_DAYS = 3                         # PA-14 of 11.1
ASSUMED_CUT_DAYS = 7                              # 10.9: an RC cut at G-7 when the file is missing (choice 2)
DETECTOR_OK = "ok"
PROVEN = ("proven_no_sends", "proven_declared_sends", "proven_no_session")     # choice 6
SHARDS_WITHOUT_PROOF = ("lost", "superseded")
DISPOSITION_OUTCOMES = ("collateral", "invariant_violation", "assertion_failed", "needs_human")   # 10.9 G-N
SOAK_GLOB = "flow/*/soak/c*"                      # 10.11: flow/<workspace-surface-id>/soak/c<nn>
SUMMARY_KEY = "release_gate"                      # runs.summary_json key of the deadlines resolved at RC cut
CONDITIONS = (
    ("run", "RC run finished and not superseded"),
    ("detector", "detector_state ok after the last Nightly run, no canary_path_divergence, no canaries pending"),
    ("coverage", "every tier-1 cell covered or its not_run explained"),
    ("quarantine", "quarantine under 2 percent of tier-1 checkpoints"),
    ("dispositions", "no tier-1 collateral, invariant_violation, assertion_failed or needs_human without a "
                     "disposition"),
    ("baselines", "no pending tier-1 baseline candidate and no tier-1 needs_baseline without an approved baseline"),
    ("soak", "soak within budget or its findings dispositioned"),
    ("fire_drill", "fire drill passed (no drill_failed, no drill_order_placed)"),
    ("proof", "post-run proof PROVEN on every RC shard"),
)
TITLES = dict(CONDITIONS)
EVIDENCE_LIMIT = 25                               # evidence lines listed per condition before "and k more"


class ReleaseGateError(ValueError):
    """`policy/release_gate.yaml` is invalid, or a train cannot be resolved."""

    def __init__(self, problems: Iterable[tuple[str, str]] | str):
        self.problems = [("", problems)] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(f"{p}: {m}" if p else m for p, m in self.problems))


# -- the file (4.16) -------------------------------------------------------------------------------------------------

def _day(value: Any) -> date:
    """A YAML date (PyYAML reads YYYY-MM-DD as a date) or its ISO text."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


@dataclass(frozen=True)
class Train:
    """One `trains` entry: GA and RC cut dates, and the train's own N when it overrides the default."""

    train: str
    ga_date: date
    rc_cut_date: date
    business_days: int | None = None


def business_days_before(d: date, n: int, clock: BusinessClock | None = None) -> date:
    """The Nth business day before `d`, `d` not counted (choice 1): G-N for a GA on `d`."""
    c = clock or BusinessClock()
    left = n
    while left > 0:
        d -= timedelta(days=1)
        if c.is_business_day(d):
            left -= 1
    return d


def business_days_after(d: date, n: int, clock: BusinessClock | None = None) -> date:
    """The Nth business day after `d`, `d` not counted: GA for an RC cut on `d` at G-n (choice 2)."""
    c = clock or BusinessClock()
    left = n
    while left > 0:
        d += timedelta(days=1)
        if c.is_business_day(d):
            left -= 1
    return d


def config_problems(doc: Any) -> list[tuple[str, str]]:
    """L-XREF-23 (4.15): train names are unique; `default_business_days` and every `business_days` are at least 1;
    every `rc_cut_date` is earlier than its `ga_date` minus N business days of the XNYS calendar (7.10), N being the
    train's `business_days` or the default. Each problem is (path, message)."""
    if not isinstance(doc, Mapping):
        return [("", "policy/release_gate.yaml is a mapping (4.16)")]
    out: list[tuple[str, str]] = []
    default = doc.get("default_business_days", DEFAULT_BUSINESS_DAYS)
    if not isinstance(default, int) or isinstance(default, bool) or default < 1:
        out.append(("default_business_days", f"default_business_days is {default!r}; N is at least 1 business day"))
        default = DEFAULT_BUSINESS_DAYS
    seen: set[str] = set()
    for i, t in enumerate(doc.get("trains") or ()):
        at = f"trains[{i}]"
        if not isinstance(t, Mapping):
            out.append((at, "a train is a mapping {train, ga_date, rc_cut_date, business_days?}"))
            continue
        name = str(t.get("train"))
        if name in seen:
            out.append((f"{at}.train", f"train {name!r} is listed twice; train names are unique"))
        seen.add(name)
        n = t.get("business_days", default)
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            out.append((f"{at}.business_days", f"business_days is {n!r}; N is at least 1 business day"))
            n = default
        try:
            ga, cut = _day(t.get("ga_date")), _day(t.get("rc_cut_date"))
        except (TypeError, ValueError):
            out.append((at, f"train {name!r}: ga_date and rc_cut_date are dates (YYYY-MM-DD)"))
            continue
        signoff = business_days_before(ga, n)
        if not cut < signoff:
            out.append((f"{at}.rc_cut_date", f"train {name!r}: rc_cut_date {cut} is not earlier than GA {ga} minus "
                                             f"{n} business days ({signoff}, XNYS)"))
    return out


@dataclass(frozen=True)
class RcDeadlines:
    """A train resolved at RC cut (11.2, 10.9): N, GA, RC cut, the G-N sign-off time and the RC start deadline."""

    train: str | None
    business_days: int
    ga_date: date
    rc_cut_date: date
    signoff_at: datetime                          # UTC, choice 1
    start_deadline: datetime                      # signoff_at minus 8 hours (11.2)
    assumed: tuple[str, ...] = ()                 # "N = 3", "RC cut at G-7" when assumed (choice 2)
    source: str = "policy"                        # "policy", "unlisted_train" or "missing_file"

    @property
    def signoff_date(self) -> date:
        return cal.ny(self.signoff_at).date()

    def header_line(self) -> str | None:
        """The RC report header line naming the assumptions (4.16, 11.7 row 12); None when nothing was assumed."""
        if not self.assumed:
            return None
        what = " and ".join(self.assumed) + (" are" if len(self.assumed) > 1 else " is") + " assumed"
        if self.source == "missing_file":
            return f"{what}: no {POLICY_PATH}"
        return f"{what}: train {self.train} is not in {POLICY_PATH}"

    def line(self) -> str:
        """`Train 2026.12: GA 2026-12-10, RC cut 2026-12-01, N = 3 business days, sign-off due Mon 2026-12-07 17:00
        EST, RC start deadline Mon 2026-12-07 09:00 EST`."""
        name = f"Train {self.train}" if self.train else "Release train"
        return (f"{name}: GA {self.ga_date}, RC cut {self.rc_cut_date}, N = {self.business_days} business days, "
                f"sign-off due {cal.fmt_ny(self.signoff_at)}, RC start deadline {cal.fmt_ny(self.start_deadline)}")

    def to_dict(self) -> dict[str, Any]:
        return {"train": self.train, "business_days": self.business_days, "ga_date": self.ga_date.isoformat(),
                "rc_cut_date": self.rc_cut_date.isoformat(), "signoff_at": _iso(self.signoff_at),
                "start_deadline": _iso(self.start_deadline), "assumed": list(self.assumed), "source": self.source}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "RcDeadlines":
        return cls(d.get("train"), int(d["business_days"]), _day(d["ga_date"]), _day(d["rc_cut_date"]),
                   _aware(d["signoff_at"]), _aware(d["start_deadline"]), tuple(d.get("assumed") or ()),
                   str(d.get("source") or "policy"))


def _iso(at: datetime) -> str:
    return cal.to_utc(at).isoformat(timespec="seconds")


def _aware(text: str) -> datetime:
    return cal.to_utc(datetime.fromisoformat(text))


@dataclass(frozen=True)
class ReleaseGatePolicy:
    """`policy/release_gate.yaml` as the Reporter and the release gate read it (4.16)."""

    default_business_days: int = DEFAULT_BUSINESS_DAYS
    trains: tuple[Train, ...] = ()
    owner: str | None = None                      # the release manager (9.12)
    present: bool = True                          # False: no file; N = 3 and an RC cut at G-7 assumed

    def train(self, name: str | None) -> Train | None:
        return next((t for t in self.trains if t.train == name), None)

    def business_days(self, train: str | None) -> int:
        """N for the train: its own `business_days`, else the default (PA-14)."""
        t = self.train(train)
        return t.business_days if t is not None and t.business_days is not None else self.default_business_days

    def rc_deadlines(self, train: str | None, *, cut_at: datetime | None = None,
                     clock: BusinessClock | None = None) -> RcDeadlines:
        """Resolve `train` at RC cut (choices 1 and 2). `cut_at` is the RC cut instant (the RC run's trigger); it is
        required when the train has no GA date in the file."""
        c = clock or BusinessClock()
        n = self.business_days(train)
        t = self.train(train)
        assumed: list[str] = []
        if t is not None:
            ga, cut, source = t.ga_date, cal.ny(cut_at).date() if cut_at is not None else t.rc_cut_date, "policy"
        else:
            if cut_at is None:
                raise ReleaseGateError(f"train {train!r} has no GA date in {POLICY_PATH} and no RC cut time was given "
                                       f"to place GA at G-{ASSUMED_CUT_DAYS}")
            cut = cal.ny(cut_at).date()
            ga = business_days_after(cut, ASSUMED_CUT_DAYS, c)
            source = "missing_file" if not self.present else "unlisted_train"
            if not self.present:
                assumed.append(f"N = {n}")
            assumed.append(f"RC cut at G-{ASSUMED_CUT_DAYS}")
        signoff_day = business_days_before(ga, n, c)
        signoff_at = cal.wall_time(signoff_day, c.closes)
        return RcDeadlines(train, n, ga, cut, signoff_at, signoff_at - RC_SIGNOFF_LEAD, tuple(assumed), source)


def from_doc(doc: Mapping[str, Any]) -> ReleaseGatePolicy:
    """A loaded `policy/release_gate.yaml`; raises `ReleaseGateError` with the L-XREF-23 problems."""
    problems = config_problems(doc)
    if problems:
        raise ReleaseGateError(problems)
    trains = tuple(Train(str(t["train"]), _day(t["ga_date"]), _day(t["rc_cut_date"]), t.get("business_days"))
                   for t in doc.get("trains") or ())
    owner = doc.get("owner")
    return ReleaseGatePolicy(int(doc.get("default_business_days", DEFAULT_BUSINESS_DAYS)), trains,
                             None if owner is None else str(owner))


def load(path: str | Path) -> ReleaseGatePolicy:
    """Read `policy/release_gate.yaml`; a missing file gives the assumed policy (N = 3, RC cut at G-7)."""
    p = Path(path)
    if not p.exists():
        return ReleaseGatePolicy(present=False)
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(doc, Mapping):
        raise ReleaseGateError(f"{p} is not a mapping (4.16)")
    return from_doc(doc)


def rc_deadlines(policy: ReleaseGatePolicy, train: str | None, *, cut_at: datetime | None = None,
                 clock: BusinessClock | None = None) -> RcDeadlines:
    """`policy.rc_deadlines(train, cut_at=...)`."""
    return policy.rc_deadlines(train, cut_at=cut_at, clock=clock)


def record_deadlines(store: Store, run_id: str, deadlines: RcDeadlines) -> None:
    """Keep the deadlines resolved at RC cut on the RC run (`summary_json["release_gate"]`, 11.7 row 12)."""
    store.update_summary(run_id, {SUMMARY_KEY: deadlines.to_dict()})


def stored_deadlines(store: Store, run_id: str) -> RcDeadlines | None:
    """The deadlines `record_deadlines` kept on the run; None when none were recorded."""
    d = store.run_summary(run_id).get(SUMMARY_KEY)
    return RcDeadlines.from_dict(d) if isinstance(d, Mapping) else None


# -- the four-input RC gate (1.5, 9.11, 11.5) ------------------------------------------------------------------------

@dataclass(frozen=True)
class RcGate:
    """Whether the RC gate is blocked and why (the run-health line of 9.11 and 11.5)."""

    blocked: bool
    reasons: tuple[str, ...]


def quarantine_blocks(share: float) -> bool:
    """Strictly above 2 percent of tier-1 checkpoints quarantined blocks the RC gate (10.5)."""
    return share > QUARANTINE_CEILING


def rc_gate(*, quarantine_share: float, detector_state: str, tier1_coverage: float | None,
            tier1_needs_baseline: int = 0) -> RcGate:
    """The RC gate from its four inputs: quarantine share (a fraction), canary detector state, tier-1 coverage percent
    (None when nothing was comparable) and the tier-1 `needs_baseline` count."""
    reasons = []
    if quarantine_blocks(quarantine_share):
        reasons.append(f"quarantine {100 * quarantine_share:.1f} percent of tier-1 checkpoints, above 2 percent")
    if detector_state != DETECTOR_OK:
        reasons.append(f"canary detector state {detector_state}")
    if tier1_coverage is None or tier1_coverage < 100:
        reasons.append("tier-1 coverage " + ("not computed" if tier1_coverage is None
                                             else f"{tier1_coverage:.1f} percent, below 100 percent"))
    if tier1_needs_baseline:
        reasons.append(f"{tier1_needs_baseline} tier-1 checkpoints needs_baseline")
    return RcGate(bool(reasons), tuple(reasons))


# -- the G-N conditions (10.9) ----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Condition:
    """One G-N condition: its key (the sign-off refusal name), title, verdict and evidence lines."""

    key: str
    passed: bool
    evidence: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return TITLES[self.key]

    def line(self) -> str:
        """`[pass] title` or `[FAIL] title`."""
        return f"[{'pass' if self.passed else 'FAIL'}] {self.title}"


@dataclass(frozen=True)
class GateResult:
    """Every G-N condition of one RC run, in checklist order (10.9)."""

    run_id: str
    conditions: tuple[Condition, ...]
    evaluated_at: datetime
    deadlines: RcDeadlines | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)    # superseded_by, last_nightly, rc_build, quarantine

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.conditions)

    @property
    def failing(self) -> tuple[Condition, ...]:
        return tuple(c for c in self.conditions if not c.passed)

    def condition(self, key: str) -> Condition:
        return next(c for c in self.conditions if c.key == key)

    def checklist(self) -> list[str]:
        """The release gate checklist of the RC report (9.4): one line per condition, its evidence indented."""
        out = []
        for c in self.conditions:
            out.append(c.line())
            out += [f"    {e}" for e in c.evidence]
        return out


def _limit(lines: list[str]) -> tuple[str, ...]:
    if len(lines) <= EVIDENCE_LIMIT:
        return tuple(lines)
    return tuple(lines[:EVIDENCE_LIMIT]) + (f"and {len(lines) - EVIDENCE_LIMIT} more",)


def last_nightly(store: Store) -> str | None:
    """The latest finished Nightly run by start time (choice 7); None before any."""
    rows = store.query("SELECT run_id FROM runs WHERE run_type = 'nightly' AND lifecycle = 'finished' "
                       "ORDER BY started_at DESC, run_id DESC LIMIT 1")
    return rows[0][0] if rows else None


def superseded_by(store: Store, run_id: str) -> str | None:
    """The newer RC build that superseded `run_id` (`aborted_budget (reason: superseded)`, 9.4, 10.9), else None.

    Read from `summary_json["superseded_by"]["build"]` (the queue's `Supersede.by_build`, written by the
    integrator), else `abort_detail` when it names a build, else the candidate build of the first later RC run."""
    r = store.query("SELECT outcome, abort_reason, abort_detail, started_at, summary_json FROM runs WHERE run_id = ?",
                    (run_id,))
    if not r or (r[0]["outcome"], r[0]["abort_reason"]) != ("aborted_budget", "superseded"):
        return None
    by = json.loads(r[0]["summary_json"] or "{}").get("superseded_by")
    if isinstance(by, Mapping) and by.get("build"):
        return str(by["build"])
    detail = r[0]["abort_detail"]
    if detail and store.query("SELECT 1 FROM builds WHERE build_id = ?", (detail,)):
        return str(detail)
    later = store.query(
        "SELECT p.candidate_build_id FROM runs x JOIN build_pairs p ON p.pair_id = x.pair_id WHERE x.run_type = 'rc' "
        "AND x.run_id <> ? AND x.started_at > ? ORDER BY x.started_at, x.run_id LIMIT 1", (run_id, r[0]["started_at"]))
    return str(later[0][0]) if later else "a newer RC build"


def _run_condition(store: Store, run_id: str, facts: dict[str, Any]) -> Condition:
    rows = store.query("SELECT run_type, lifecycle, outcome, abort_reason FROM runs WHERE run_id = ?", (run_id,))
    if not rows:
        return Condition("run", False, (f"unknown run {run_id}",))
    r = rows[0]
    if r["run_type"] != "rc":
        return Condition("run", False, (f"run {run_id} is a {r['run_type']} run, not an RC run",))
    if r["lifecycle"] != "finished":
        return Condition("run", False, (f"RC run {run_id} is {r['lifecycle']}",))
    said = r["outcome"] + (f" (reason: {r['abort_reason']})" if r["abort_reason"] else "")
    by = superseded_by(store, run_id)
    facts["superseded_by"] = by
    if by is not None:
        return Condition("run", False, (f"RC run {run_id} ended {said}: superseded by {by}; it cannot be signed",))
    return Condition("run", True, (f"RC run {run_id} ended {said}",))


def _detector_condition(store: Store, facts: dict[str, Any]) -> Condition:
    nightly = last_nightly(store)
    facts["last_nightly"] = nightly
    ds = read_detector_state(store)
    bad: list[str] = []
    ok: list[str] = []
    if nightly is None:
        bad.append("no finished Nightly run")
    if ds is None:
        bad.append("no detector state recorded")
    else:
        if ds.state != DETECTOR_OK:
            bad.append(f"detector_state {ds.state} (Nightly {ds.run_id}" + (f", since {ds.since_text})"
                                                                            if ds.since_text else ")"))
        elif nightly is not None and ds.run_id != nightly:
            bad.append(f"detector_state was written by Nightly {ds.run_id}, not by the last Nightly {nightly}")
        else:
            ok.append(f"detector_state ok (Nightly {ds.run_id})")
        if ds.divergence:
            bad.append(f"path equivalence diverged in Nightly {ds.run_id}")
    if nightly is not None:
        n = store.query("SELECT count(*) FROM outcomes WHERE run_id = ? AND outcome = 'harness_error' "
                        "AND reason = 'canary_path_divergence'", (nightly,))[0][0]
        if n:
            bad.append(f"Nightly {nightly} has {n} harness_error (reason: canary_path_divergence) rows")
    pending = store.get_system_state("canaries_pending")
    if pending:
        bad.append(f"canaries pending: {pending}")
    return Condition("detector", not bad, tuple(bad or ok))


def _coverage_condition(store: Store, run_id: str) -> Condition:
    cells = store.query("SELECT surface_id, persona, entitlement, state, not_run_reason FROM coverage_cells "
                        "WHERE run_id = ? AND tier = 1 AND state <> 'not_registered' "
                        "ORDER BY surface_id, persona, entitlement", (run_id,))
    if not cells:
        return Condition("coverage", False, ("no coverage matrix stored for the RC run (coverage_cells, 2.12)",))
    counted = [c for c in cells if not (c["state"] == "not_run" and c["not_run_reason"] in DENOMINATOR_EXCLUDED)]
    missing = [c for c in counted if c["state"] != "covered"]
    head = (f"tier 1: {len(counted) - len(missing)} of {len(counted)} cells covered"
            + (f" ({100 * (len(counted) - len(missing)) / len(counted):.1f} percent)" if counted else "")
            + f"; {len(cells) - len(counted)} explained not_run (entitlement or scope)")
    lines = [f"{c['surface_id']} x {c['persona']} x {c['entitlement']}: not_run ({c['not_run_reason']})"
             for c in missing]
    return Condition("coverage", not missing, (head, *_limit(lines)))


def quarantine_share(store: Store, run_id: str) -> QuarantineShare:
    """Tier-1 checkpoints `not_run (quarantine)` over the run's tier-1 checkpoints, less those `not_run
    (entitlement)` or `not_run (scope)` (choice 4)."""
    rows = store.query(
        "SELECT c.checkpoint_id, o.outcome, o.reason FROM checkpoints c "
        "LEFT JOIN outcomes o ON o.run_id = :run AND o.checkpoint_id = c.checkpoint_id "
        "WHERE c.tier = 1 AND (o.run_id IS NOT NULL OR c.checkpoint_id IN "
        "(SELECT checkpoint_id FROM run_scope WHERE run_id = :run)) AND COALESCE(o.synthetic, 0) = 0",
        {"run": run_id})
    excluded = {r[0] for r in rows if r[1] == "not_run" and r[2] in DENOMINATOR_EXCLUDED}
    counted = {r[0] for r in rows} - excluded
    quarantined = {r[0] for r in rows if r[1] == "not_run" and r[2] == "quarantine"}
    return QuarantineShare(len(quarantined), len(counted))


def _quarantine_condition(store: Store, run_id: str, facts: dict[str, Any]) -> Condition:
    q = quarantine_share(store, run_id)
    facts["quarantine_share"] = q.share
    line = (f"{q.quarantined} of {q.checkpoints} tier-1 checkpoints not_run (quarantine): {100 * q.share:.1f} percent"
            f" (ceiling 2 percent)")
    return Condition("quarantine", not quarantine_blocks(q.share), (line,))


def _dispositions_condition(store: Store, run_id: str, now: datetime, clock: BusinessClock,
                            deadlines: RcDeadlines | None) -> Condition:
    marks = ", ".join(f"'{o}'" for o in DISPOSITION_OUTCOMES)
    rows = store.query(
        f"SELECT o.checkpoint_id, o.outcome, o.reason, o.reason_detail FROM outcomes o "
        f"JOIN checkpoints c ON c.checkpoint_id = o.checkpoint_id "
        f"WHERE o.run_id = ? AND c.tier = 1 AND o.synthetic = 0 AND o.decision_id IS NULL AND o.outcome IN ({marks}) "
        f"AND NOT EXISTS (SELECT 1 FROM findings f WHERE f.run_id = o.run_id AND f.checkpoint_id = o.checkpoint_id "
        f"AND f.synthetic = 0) ORDER BY o.checkpoint_id", (run_id,))
    lines = [f"{r['checkpoint_id']}: {_said(r['outcome'], r['reason'], r['reason_detail'])}, no finding and no "
             f"decision" for r in rows]
    due = {run_id: deadlines.signoff_at} if deadlines is not None else None
    lines += [f"finding {x.finding_id}: {x.reason}"
              for x in rc_signoff_refusals(store, run_id, now, clock=clock, rc_deadlines=due)]
    return Condition("dispositions", not lines, _limit(lines) if lines else ("every tier-1 item has a disposition",))


def _said(outcome: str, reason: str | None, detail: str | None = None) -> str:
    if reason is None:
        return outcome
    return f"{outcome} (reason: {reason}" + (f":{detail}" if detail else "") + ")"


def _baselines_condition(store: Store, run_id: str, lineage: str) -> Condition:
    pending = store.query(
        "SELECT b.baseline_id, b.checkpoint_id, b.source_run_id FROM baselines b "
        "JOIN checkpoints c ON c.checkpoint_id = b.checkpoint_id "
        "WHERE b.lineage = ? AND b.state = 'candidate' AND c.tier = 1 AND c.retired_in_build IS NULL "
        "ORDER BY b.checkpoint_id, b.baseline_id", (lineage,))
    lines = [f"{r['checkpoint_id']}: candidate #{r['baseline_id']}" + (f" from run {r['source_run_id']}"
                                                                         if r["source_run_id"] else "") + " pending"
             for r in pending]
    nb = store.query(
        "SELECT o.checkpoint_id, o.reason FROM outcomes o JOIN checkpoints c ON c.checkpoint_id = o.checkpoint_id "
        "WHERE o.run_id = ? AND c.tier = 1 AND o.synthetic = 0 AND o.outcome = 'needs_baseline' AND NOT EXISTS "
        "(SELECT 1 FROM baselines b WHERE b.checkpoint_id = o.checkpoint_id AND b.lineage = ? AND "
        "b.state = 'approved') ORDER BY o.checkpoint_id", (run_id, lineage))
    lines += [f"{r['checkpoint_id']}: {_said('needs_baseline', r['reason'])}, no approved baseline on {lineage}"
              for r in nb]
    return Condition("baselines", not lines, _limit(lines) if lines else
                     (f"no tier-1 candidate pending on lineage {lineage}",))


def _soak_condition(store: Store, run_id: str) -> Condition:
    cycles = store.query(
        "SELECT s.checkpoint_id, o.outcome, o.reason, o.reason_detail, o.decision_id FROM run_scope s "
        "LEFT JOIN outcomes o ON o.run_id = s.run_id AND o.checkpoint_id = s.checkpoint_id "
        "WHERE s.run_id = :run AND s.checkpoint_id GLOB :glob "
        "UNION SELECT o.checkpoint_id, o.outcome, o.reason, o.reason_detail, o.decision_id FROM outcomes o "
        "WHERE o.run_id = :run AND o.checkpoint_id GLOB :glob AND o.synthetic = 0 ORDER BY 1",
        {"run": run_id, "glob": SOAK_GLOB})
    if not cycles:
        return Condition("soak", False, ("soak did not run: no soak cycle checkpoint in the RC run (10.11)",))
    lines = []
    unreached = [c for c in cycles if c["outcome"] is None or c["outcome"] == "not_run"]
    if unreached:
        lines.append(f"{len(unreached)} of {len(cycles)} soak cycles did not run or did not finish: "
                     + ", ".join(sorted({_said(c['outcome'] or 'no outcome', c['reason']) for c in unreached})))
    open_findings = store.query(
        "SELECT natural_key, kind, checkpoint_id FROM findings WHERE run_id = ? AND checkpoint_id GLOB ? "
        "AND synthetic = 0 AND triage_state = 'open' AND decision_id IS NULL ORDER BY natural_key",
        (run_id, SOAK_GLOB))
    lines += [f"finding {r['natural_key']} ({r['kind']}) on {r['checkpoint_id']} without a disposition"
              for r in open_findings]
    with_finding = {r["checkpoint_id"] for r in store.query(
        "SELECT DISTINCT checkpoint_id FROM findings WHERE run_id = ? AND checkpoint_id GLOB ? AND synthetic = 0",
        (run_id, SOAK_GLOB))}
    lines += [f"{c['checkpoint_id']}: {_said(c['outcome'], c['reason'], c['reason_detail'])}, no finding and no "
              f"decision" for c in cycles
              if c["outcome"] == "assertion_failed" and c["decision_id"] is None
              and c["checkpoint_id"] not in with_finding]
    breaches = sum(c["outcome"] == "assertion_failed" for c in cycles)
    head = (f"{len(cycles)} soak cycles; {breaches} budget breaches" if breaches or lines
            else f"{len(cycles)} soak cycles within budget")
    return Condition("soak", not lines, (head, *_limit(lines)))


def _fire_drill_condition(store: Store, nightly: str | None) -> Condition:
    if nightly is None:
        return Condition("fire_drill", False, ("no finished Nightly run: no fire drill result",))
    rows = [r for r in store.canary_results(nightly) if r.kind == "fire_drill"]
    run = store.query("SELECT outcome, abort_reason FROM runs WHERE run_id = ?", (nightly,))[0]
    failed = store.query("SELECT count(*) FROM outcomes WHERE run_id = ? AND outcome = 'harness_error' "
                         "AND reason = 'drill_failed'", (nightly,))[0][0]
    placed = (run["outcome"], run["abort_reason"]) == ("aborted_safety", "drill_order_placed") \
        or any(r.produced_reason == "drill_order_placed" for r in rows)
    passed = bool(rows) and not placed and not failed and all(r.state == "detected" for r in rows)
    text = f"Nightly {nightly}: " + drill_summary(rows, nightly_run_id=nightly)
    if placed and not any(r.produced_reason == "drill_order_placed" for r in rows):
        text = f"Nightly {nightly}: aborted_safety (reason: drill_order_placed)"
    extra = (f"{failed} harness_error (reason: drill_failed) rows in Nightly {nightly}",) if failed else ()
    return Condition("fire_drill", passed, (text, *extra))


def _proof_condition(store: Store, run_id: str) -> Condition:
    shards = [r[0] for r in store.query(
        "SELECT shard_id FROM shards WHERE run_id = ? AND state NOT IN ('lost', 'superseded') ORDER BY shard_id",
        (run_id,))]
    proofs = {p.shard_id: p for p in store.post_run_proofs(run_id)}
    wanted: list[str | None] = list(shards) if shards else [None]
    bad, good = [], []
    for sid in wanted:
        name = f"shard {sid}" if sid else "run level"
        p = proofs.get(sid)
        if p is None:
            bad.append(f"{name}: no post-run proof")
        elif p.verdict not in PROVEN:
            cleared = f", cleared by hand by {p.verified_by}" if p.verified_by else ""
            bad.append(f"{name}: {p.verdict}" + (f" ({p.reason})" if p.reason else "") + cleared)
        else:
            good.append(sid)
    if bad:
        return Condition("proof", False, _limit(bad))
    return Condition("proof", True, (f"PROVEN on {len(good)} of {len(wanted)} " +
                                     ("shards" if shards else "run-level proofs"),))


def evaluate(store: Store, rc_run_id: str, *, now: datetime | None = None, lineage: str = "released",
             deadlines: RcDeadlines | None = None, clock: BusinessClock | None = None) -> GateResult:
    """Every G-N condition of 10.9 for `rc_run_id` at `now` (default: the current time), with evidence. `deadlines`
    defaults to the ones recorded on the run at RC cut (`stored_deadlines`); `lineage` is the release channel whose
    candidates and approved baselines `baselines` reads (9.8)."""
    at = cal.to_utc(now) if now is not None else datetime.now(cal.UTC)
    c = clock or BusinessClock()
    known = store.query("SELECT 1 FROM runs WHERE run_id = ?", (rc_run_id,))
    dl = deadlines if deadlines is not None else (stored_deadlines(store, rc_run_id) if known else None)
    facts: dict[str, Any] = {}
    conditions = [_run_condition(store, rc_run_id, facts)]
    if known:
        conditions += [_detector_condition(store, facts), _coverage_condition(store, rc_run_id),
                       _quarantine_condition(store, rc_run_id, facts),
                       _dispositions_condition(store, rc_run_id, at, c, dl),
                       _baselines_condition(store, rc_run_id, lineage), _soak_condition(store, rc_run_id),
                       _fire_drill_condition(store, facts.get("last_nightly")), _proof_condition(store, rc_run_id)]
    return GateResult(rc_run_id, tuple(conditions), at, dl, facts)
