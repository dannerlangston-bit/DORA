"""The Phase 0 decision rules DR-01, DR-02, DR-04 and DR-05 as pure evaluators, and `sentinel phase0 decide` (11.8
Phase 0 spike plan: seven decision records; 11.10 R23).

The measurements need the rig; the rules do not. Each `decide_drNN(evidence)` takes what the spike measured and
returns a `Verdict`: the branch the record's decision rule gives (A or B), or None when the evidence cannot decide
(the record stays undecided and its 11.8 default takes over at its deadline, `sentinel.branches`), with one evidence
line per fact the rule read. `write_branch` records a decided verdict in `phase0/branches.yaml` (4.16), keeping the
file's comments, and checks the result with `branches.parse`; `main(argv)` is `sentinel phase0 decide`.

- DR-01 Java Access Bridge exposure: `java --list-modules` without `jdk.accessibility` is B. Otherwise A when on all
  five surfaces (order ticket, confirm dialog, option chain, Monitor grid, Active Trader ladder) at least 80 percent
  of the visible controls expose name, role and bounds within 2 px of the template-located control and the dump took
  under 2 s; else B, with JAB optional on the panels that passed (`detail["jab_panels"]`).
- DR-02 Deterministic data fixture: a written yes with delivery inside Phase 1 week 4 is A, otherwise B. Inside B,
  OnDemand is the frozen provider when at least 18 of 20 OnDemand launches settled and two paused captures 10 minutes
  apart are pixel-identical outside the clock mask, else masked_live (`detail["frozen_provider"]`).
- DR-04 Pinned build install: A when build id, JRE release and vmoptions hash are unchanged over five launches on two
  days with no update or agreement dialog; else B. An artifact not delivered by day 2 is the default, B. The record
  also pins the main-window title regex (`detail["title_regex"]`, written to `pinned_install.title_regex`) and how
  the launcher hosts the JVM (`detail["launcher"]`, 10.2).
- DR-05 A/A noise floor: over the Phase 0 smoke census (at least 60 census checkpoints across the five tier A
  workspaces) and five starter flows, A when at least 95 percent of frozen checkpoints have zero diff pixels outside
  masks across all six captures and none exceeds 0.1 percent; else B, with the non-zero frozen checkpoints as the fix
  list (`detail["fix_list"]`). A calibration that did not complete is the default, B.

Choices where the spec is ambiguous (each is reported):

1. "Within 2 px" (DR-01) compares each edge of the JAB bounds (left, top, right, bottom) with the template-located
   control's; a missing name, role or bounds is not exposed. A surface with no visible control cannot reach 80
   percent. A surface missing from the evidence leaves the record undecided.
2. DR-02 with no written answer yet (`written_yes` None) is undecided: the default (B with masked_live) holds at the
   deadline through `sentinel.branches`. OnDemand needs at least 20 launches; the first 20 are the sample, and a
   paused-capture comparison that was not made does not qualify OnDemand. `phase0/branches.yaml` records the letter
   only, so the OnDemand result is in the evidence and `detail` (reported: the file has no field for it).
3. DR-04: fewer than five launches, or launches on fewer than two days, leave the record undecided; a read that came
   back empty counts as changed (the rule needs it unchanged). Any dialog seen at a launch is an update or agreement
   dialog. A `title_regex` that does not compile is refused.
4. DR-05: a checkpoint's evidence is the largest changed-pixel count outside masks and the largest whole-window diff
   percent (the Diff Engine's `whole.pixel_fraction` x 100, 4.13) over its 15 A/A pairs; "none exceeds 0.1 percent"
   is every frozen checkpoint at or below 0.1. The scope check counts distinct census workspaces (the smoke census
   runs in the tier A workspaces, 2.3) and distinct flow ids among the flow checkpoints; a short scope or no frozen
   checkpoint leaves the record undecided.
5. `write_branch` sets `branch`, `decided_by` and `decided_at` on the record (and `title_regex` for DR-04), replacing
   the record's lines with one flow-style line and keeping its trailing comment; the deadline goes, since a decided
   record has none (11.8). The command exits 0 when the record is decided (and written when `--branches` is given),
   3 when the evidence cannot decide, 2 on a usage or input error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from . import branches
from .lint.repo import load_yaml_text

DR01_SURFACES = ("order_ticket", "confirm_dialog", "option_chain", "monitor_grid", "active_trader_ladder")   # 11.8
DR01_SHARE, DR01_BOUNDS_PX, DR01_DUMP_S = 0.80, 2, 2.0
DR02_LAUNCHES, DR02_SETTLED = 20, 18
DR04_LAUNCHES, DR04_DAYS = 5, 2
DR05_CENSUS, DR05_WORKSPACES, DR05_FLOWS = 60, 5, 5
DR05_ZERO_SHARE, DR05_MAX_PCT = 0.95, 0.1
EXIT_DECIDED, EXIT_USAGE, EXIT_UNDECIDED = 0, 2, 3


@dataclass(frozen=True)
class Verdict:
    """What one decision rule gives: the branch (None: undecided) and the evidence it read (11.8)."""

    record: str                           # DR-01 ...
    key: str                              # the phase0/branches.yaml key (`branches.RECORD_IDS`)
    branch: str | None
    evidence: tuple[str, ...]
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def decided(self) -> bool:
        return self.branch is not None

    def lines(self) -> list[str]:
        head = f"{self.record} {self.key}: " + (f"branch {self.branch}" if self.decided else
                                                f"undecided (default {branches.DEFAULTS[self.key]} at the deadline)")
        return [head] + [f"  {e}" for e in self.evidence]


def _verdict(key: str, branch: str | None, evidence: list[str], **detail: Any) -> Verdict:
    return Verdict(branches.RECORD_IDS[key], key, branch, tuple(evidence), detail)


# -- DR-01 ------------------------------------------------------------------------------------------------------

Box = tuple[int, int, int, int]                   # x, y, w, h


@dataclass(frozen=True)
class ControlRead:
    """One visible control in a JAB dump beside its template-located bounds."""

    control_id: str
    name: str | None
    role: str | None
    jab_bounds: Box | None
    template_bounds: Box

    def exposed(self) -> bool:
        """Name, role and bounds, each edge within 2 px of the template's (choice 1)."""
        if not (self.name and self.role and self.jab_bounds):
            return False
        def edges(b: Box) -> tuple[int, int, int, int]:
            return b[0], b[1], b[0] + b[2], b[1] + b[3]
        pairs = zip(edges(self.jab_bounds), edges(self.template_bounds), strict=True)
        return all(abs(a - b) <= DR01_BOUNDS_PX for a, b in pairs)


@dataclass(frozen=True)
class SurfaceDump:
    """The JAB tree dump of one of the five DR-01 surfaces."""

    surface: str
    controls: tuple[ControlRead, ...]
    dump_s: float

    @property
    def share(self) -> float:
        return sum(c.exposed() for c in self.controls) / len(self.controls) if self.controls else 0.0

    @property
    def passed(self) -> bool:
        return self.share >= DR01_SHARE and self.dump_s < DR01_DUMP_S


@dataclass(frozen=True)
class Dr01Evidence:
    """`module_present`: `java --list-modules` lists `jdk.accessibility` (None: not checked)."""

    module_present: bool | None
    dumps: tuple[SurfaceDump, ...] = ()

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Dr01Evidence:
        dumps = tuple(SurfaceDump(s["surface"], tuple(ControlRead(
            c["control_id"], c.get("name"), c.get("role"), tuple(c["jab_bounds"]) if c.get("jab_bounds") else None,
            tuple(c["template_bounds"])) for c in s.get("controls") or ()), float(s["dump_s"]))
                      for s in d.get("dumps") or ())
        return cls(d.get("module_present"), dumps)


def decide_dr01(e: Dr01Evidence) -> Verdict:
    """DR-01 (11.8; choice 1)."""
    if e.module_present is False:
        return _verdict("jab", "B", ["java --list-modules: jdk.accessibility absent from the bundled runtime"])
    if e.module_present is None:
        return _verdict("jab", None, ["java --list-modules not checked"])
    by = {d.surface: d for d in e.dumps}
    missing = [s for s in DR01_SURFACES if s not in by]
    if missing:
        return _verdict("jab", None, [f"no dump for {', '.join(missing)}"])
    lines = [f"{s}: {100 * by[s].share:.0f} percent of {len(by[s].controls)} visible controls exposed, dump "
             f"{by[s].dump_s:.2f} s ({'pass' if by[s].passed else 'fail'})" for s in DR01_SURFACES]
    passed = [s for s in DR01_SURFACES if by[s].passed]
    branch = "A" if len(passed) == len(DR01_SURFACES) else "B"
    return _verdict("jab", branch, ["jdk.accessibility present"] + lines, jab_panels=tuple(passed))


# -- DR-02 ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Dr02Evidence:
    """The backend's written answer and, inside B, the OnDemand trial (11.8)."""

    written_yes: bool | None
    delivery: date | None = None
    phase1_week4_end: date | None = None
    launches_settled: tuple[bool, ...] = ()
    paused_identical: bool | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Dr02Evidence:
        def day(k: str) -> date | None:
            return date.fromisoformat(str(d[k])) if d.get(k) else None
        return cls(d.get("written_yes"), day("delivery"), day("phase1_week4_end"),
                   tuple(bool(x) for x in d.get("launches_settled") or ()), d.get("paused_identical"))


def decide_dr02(e: Dr02Evidence) -> Verdict:
    """DR-02 (11.8; choice 2)."""
    if e.written_yes is None:
        return _verdict("data_fixture", None, ["no written answer from the backend team yet"])
    inside = bool(e.written_yes and e.delivery and e.phase1_week4_end and e.delivery <= e.phase1_week4_end)
    if inside:
        return _verdict("data_fixture", "A", [f"written yes, delivery {e.delivery} inside Phase 1 week 4 "
                                              f"(ends {e.phase1_week4_end})"], frozen_provider="backend_fixture")
    why = "written no" if not e.written_yes else f"delivery {e.delivery or 'not dated'} after Phase 1 week 4"
    sample = e.launches_settled[:DR02_LAUNCHES]
    settled = sum(sample)
    ondemand = len(sample) == DR02_LAUNCHES and settled >= DR02_SETTLED and e.paused_identical is True
    paused = {True: "pixel-identical", False: "differ", None: "not compared"}[e.paused_identical]
    lines = [why, f"OnDemand: {settled} of {len(sample)} launches settled (needs {DR02_SETTLED} of {DR02_LAUNCHES}); "
             f"paused captures 10 minutes apart {paused} outside the clock mask",
             "OnDemand is the frozen provider" if ondemand else "masked_live for every quote-bearing checkpoint"]
    return _verdict("data_fixture", "B", lines, frozen_provider="ondemand" if ondemand else "masked_live")


# -- DR-04 ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LaunchRead:
    """One launch of the installed artifact: what the window title, JRE release file and vmoptions read."""

    day: date
    build_id: str | None
    jre_release: str | None
    vmoptions_sha256: str | None
    dialogs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dr04Evidence:
    artifact_delivered: bool
    launches: tuple[LaunchRead, ...] = ()
    title_regex: str | None = None
    launcher: str | None = None           # in_process or javaw_child (10.2)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Dr04Evidence:
        reads = tuple(LaunchRead(date.fromisoformat(str(x["day"])), x.get("build_id"), x.get("jre_release"),
                                 x.get("vmoptions_sha256"), tuple(x.get("dialogs") or ()))
                      for x in d.get("launches") or ())
        return cls(bool(d.get("artifact_delivered")), reads, d.get("title_regex"), d.get("launcher"))


def decide_dr04(e: Dr04Evidence) -> Verdict:
    """DR-04 (11.8; choice 3)."""
    if e.title_regex is not None:
        try:
            re.compile(e.title_regex)
        except re.error as exc:
            raise ValueError(f"DR-04 title_regex does not compile: {exc}") from exc
    extra = {k: v for k, v in (("title_regex", e.title_regex), ("launcher", e.launcher)) if v is not None}
    if not e.artifact_delivered:
        return _verdict("pinned_install", "B", ["pipeline artifact not delivered by day 2: the public build with the "
                                                "updater held, until row 1 of 11.7 lands"], **extra)
    days = {r.day for r in e.launches}
    if len(e.launches) < DR04_LAUNCHES or len(days) < DR04_DAYS:
        return _verdict("pinned_install", None, [f"{len(e.launches)} launches on {len(days)} days; the rule needs "
                                                 f"{DR04_LAUNCHES} over {DR04_DAYS} days"])
    lines, stable = [], True
    for name in ("build_id", "jre_release", "vmoptions_sha256"):
        values = [getattr(r, name) for r in e.launches]
        same = None not in values and len(set(values)) == 1
        stable &= same
        lines.append(f"{name}: {'unchanged' if same else 'changed or unread'} over {len(values)} launches")
    dialogs = sorted({d for r in e.launches for d in r.dialogs})
    lines.append(f"update or agreement dialogs: {', '.join(dialogs) if dialogs else 'none'}")
    return _verdict("pinned_install", "A" if stable and not dialogs else "B", lines, **extra)


# -- DR-05 ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AACheckpoint:
    """One checkpoint of the Phase 0 Calibration run: its data mode and its worst A/A pair (choice 4)."""

    checkpoint_id: str
    data_mode: str
    max_changed_px: int
    max_diff_pct: float


@dataclass(frozen=True)
class Dr05Evidence:
    checkpoints: tuple[AACheckpoint, ...]
    completed: bool = True

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Dr05Evidence:
        return cls(tuple(AACheckpoint(c["checkpoint_id"], c["data_mode"], int(c["max_changed_px"]),
                                      float(c["max_diff_pct"])) for c in d.get("checkpoints") or ()),
                   bool(d.get("completed", True)))


def decide_dr05(e: Dr05Evidence) -> Verdict:
    """DR-05 (11.8; choice 4)."""
    if not e.completed:
        return _verdict("aa_floor", "B", ["calibration did not complete: no thresholds, every region needs_human "
                                          "(reason: uncalibrated) above zero until a clean Calibration run"])
    census = [c for c in e.checkpoints if c.checkpoint_id.startswith("census/")]
    workspaces = {c.checkpoint_id.split("/")[2] for c in census}
    flows = {c.checkpoint_id.split("/")[2] for c in e.checkpoints if c.checkpoint_id.startswith("flow/")}
    scope = (f"scope: {len(census)} census checkpoints in {len(workspaces)} workspaces, {len(flows)} starter flows")
    if len(census) < DR05_CENSUS or len(workspaces) < DR05_WORKSPACES or len(flows) < DR05_FLOWS:
        return _verdict("aa_floor", None, [scope + f"; the rule needs {DR05_CENSUS} in {DR05_WORKSPACES} and "
                                                   f"{DR05_FLOWS} flows"])
    frozen = [c for c in e.checkpoints if c.data_mode == "frozen"]
    if not frozen:
        return _verdict("aa_floor", None, [scope, "no frozen checkpoint measured"])
    zero = sum(c.max_changed_px == 0 for c in frozen)
    worst = max(frozen, key=lambda c: (c.max_diff_pct, c.checkpoint_id))
    share = zero / len(frozen)
    branch = "A" if share >= DR05_ZERO_SHARE and worst.max_diff_pct <= DR05_MAX_PCT else "B"
    fixes = tuple(c.checkpoint_id for c in frozen if c.max_changed_px > 0)
    return _verdict("aa_floor", branch, [scope, f"{zero} of {len(frozen)} frozen checkpoints ({100 * share:.1f} "
                                                f"percent) have zero diff pixels outside masks (needs 95)",
                                         f"largest frozen diff {worst.max_diff_pct:g} percent on {worst.checkpoint_id} "
                                         "(none may exceed 0.1)"], fix_list=fixes)


DECIDERS: Mapping[str, tuple[Callable[[Mapping[str, Any]], Any], Callable[[Any], Verdict]]] = {
    "DR-01": (Dr01Evidence.from_dict, decide_dr01), "DR-02": (Dr02Evidence.from_dict, decide_dr02),
    "DR-04": (Dr04Evidence.from_dict, decide_dr04), "DR-05": (Dr05Evidence.from_dict, decide_dr05)}


# -- phase0/branches.yaml -----------------------------------------------------------------------------------------

def write_branch(text: str, verdict: Verdict, *, by: str, on: date) -> str:
    """The text of `phase0/branches.yaml` with `verdict` recorded (choice 5); raises `ValueError` for an undecided
    verdict and `branches.BranchesError` when the result could not drive a run."""
    if not verdict.decided:
        raise ValueError(f"{verdict.record} is undecided; nothing to record")
    if not by.strip():
        raise ValueError("a decision names who decided it (decided_by)")
    record: dict[str, Any] = {"branch": verdict.branch, "decided_by": by, "decided_at": on.isoformat()}
    if verdict.key == "pinned_install" and verdict.detail.get("title_regex"):
        record["title_regex"] = verdict.detail["title_regex"]
    flow = yaml.safe_dump(record, default_flow_style=True, sort_keys=False, width=10_000).strip()
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(rf"^{verdict.key}:(\s|$)", ln)), None)
    if start is None:
        raise ValueError(f"{branches.BRANCHES_PATH} has no {verdict.key} record")
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    comment = re.search(r"(?<=[}\s])(\s+#.*)$", lines[start])
    new = f"{verdict.key}: {flow}" + (comment.group(1) if comment else "")
    out = "\n".join(lines[:start] + [new] + lines[end:]) + ("\n" if text.endswith("\n") else "")
    parsed = branches.parse(load_yaml_text(out))
    if parsed.choices[verdict.key].branch != verdict.branch:
        raise branches.BranchesError(f"{verdict.key} did not read back as branch {verdict.branch}")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    """`sentinel phase0 decide DR-0N --evidence FILE [--branches PATH --by NAME] [--on YYYY-MM-DD] [--json]`."""
    p = argparse.ArgumentParser(prog="sentinel phase0", description="Evaluate a Phase 0 decision rule (11.8) and "
                                "record the branch in phase0/branches.yaml.")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("decide")
    d.add_argument("record", choices=sorted(DECIDERS))
    d.add_argument("--evidence", required=True, help="YAML or JSON evidence document of the spike")
    d.add_argument("--branches", help="phase0/branches.yaml to record the branch in")
    d.add_argument("--by", default="", help="who decides (decided_by)")
    d.add_argument("--on", default=date.today().isoformat(), help="decision date (decided_at)")
    d.add_argument("--json", action="store_true")
    try:
        a = p.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_DECIDED
    load, decide = DECIDERS[a.record]
    try:
        verdict = decide(load(yaml.safe_load(Path(a.evidence).read_text(encoding="utf-8")) or {}))
        on = date.fromisoformat(a.on)
        if verdict.decided and a.branches:
            path = Path(a.branches)
            path.write_text(write_branch(path.read_text(encoding="utf-8"), verdict, by=a.by, on=on), encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(f"sentinel phase0 decide: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if a.json:
        print(json.dumps({"record": verdict.record, "key": verdict.key, "branch": verdict.branch,
                          "evidence": list(verdict.evidence), "detail": dict(verdict.detail)}, default=list))
    else:
        print("\n".join(verdict.lines()))
    return EXIT_DECIDED if verdict.decided else EXIT_UNDECIDED
