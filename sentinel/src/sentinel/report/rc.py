"""The RC report's own sections, its sign-off, its frozen versions, the superseded label and escape tracking (9.4 RC
report, 9.7, 9.8, 9.11, 9.12, 10.9 G-6 to G+1, 10.11, 11.1 PA-14, 11.2; SD-86, SD-93).

The RC report is the merge report (9.4 items 1 to 9, `report.model`) plus the sections built here, from the store:

- `build_rc_report(store, run_id, version=None, inputs=RcInputs(...))` -> `RcReport`: the header (RC build, base and
  lineage, train and deadlines, the "N = 3 and RC cut at G-7 assumed" line, and on a superseded run the label
  "superseded by {build}") and the RC sections in 9.4's order: lineage comparison, Intended by Jira epic reconciled
  against the ticket export, size sweeps per gadget and Ladder F step, soak summary (10.11), Explorer findings (2.9,
  10.7), tier B migration, baseline approval status (9.8), the escape-tracking seed, the release gate checklist
  (`release_gate.evaluate`, 10.9), the decisions on the run and the sign-off block. `RcReport.text()` is the frozen
  form; `version` stamps it (None: the live, unfrozen report).
- `sign_off(store, run_id, system_owner, release_manager, accepted)` signs the RC run or raises `SignOffRefused` with
  named refusals (`SignOffRefusal.code`): `run`, `superseded`, `already_signed`, `not_a_person`, `same_person`,
  `not_role_holder`, `accepted_unknown`, `accepted_without_ticket`, and every failing G-N condition by its
  `release_gate.CONDITIONS` key (`detector`, `coverage`, `quarantine`, `dispositions`, `baselines`, `soak`,
  `fire_drill`, `proof`). A sign-off records the block (system owner, release manager, time, RC build, accepted open
  findings with tickets, the approved baseline set that becomes the next lineage baseline, and the 6.13 known
  residuals, `report.safety.RESIDUALS`, "listed in every RC sign-off") and freezes version 1.
- `new_version(store, run_id)` freezes the next version after a later decision; the block in it names the version
  signed. `versions`, `signoff_of` and `frozen_text` read them back.
- `queue_lineage_candidates(store, rc_run_id)`: the RC run's baseline candidates for the lineage set (SD-86, 9.8).
- Escape tracking (10.9 G+1, 9.11): `escape_seed` (every tier-1 surface not compared in the RC run), `classify_escape`,
  `record_escape` (an `escapes` row tagged with the checkpoint that should have shown the bug, or `not_registered`)
  and `escape_rate`.

Persistence (no table of its own; reported): the sign-off and the versions live in the RC run's `summary_json`
under `rc_signoff` and `rc_report_versions` (`Store.update_summary`), the frozen text as a `report` artifact in the
run's set when an `ArtifactStore` is given. The newer build of a superseded run is read from `summary_json
["superseded_by"]` (`release_gate.superseded_by`), which the integrator writes from the queue's `Supersede` event.

Choices where the spec is ambiguous (each is reported):

1. The system owner and the release manager are two different people, neither a service account (L-DEC-01); with a
   roster (`store.Roster.from_oncall(ops/oncall.yaml)`) each must hold the role, as holder or deputy.
2. An accepted open finding is a finding of the RC run; its ticket is the one given with it, else the finding's, else
   its decision's. Accepting a finding does not dispose of it: an undispositioned tier-1 finding still refuses
   sign-off through `dispositions`.
3. A sign-off is once per RC run (`already_signed`); a respin is a new RC run with its own sign-off (10.9). Signing
   after the G-N sign-off time is not refused; the block says "late".
4. A new version is frozen only when the report's content (everything but the version stamp) differs from the last
   frozen version; `new_version` returns None otherwise. Versions exist only after sign-off. The "Decisions" section
   lists every decision on the run, so a later decision always changes the content.
5. The approved baseline set is every `approved` baseline of the lineage at sign-off, listed by checkpoint in the
   summary and printed as its count and the sha256 of its canonical JSON.
6. Lineage candidates (SD-86) are the RC run's non-synthetic `needs_baseline` and `intended` captures, queued as
   `baselines` candidates on the lineage unless the lineage already holds that image for the checkpoint as an
   approved or candidate baseline. The integrator queues them when the RC report is published, before sign-off,
   since a pending tier-1 candidate refuses sign-off (9.8).
7. Intended by epic: the epic of an entry is the epic the ticket export gives its first listed ticket; entries whose
   tickets the export does not hold group under "not in the ticket export". Reconciliation lists export tickets with
   no manifest entry and manifest tickets absent from the export.
8. Explorer "not run, model endpoint unavailable" is printed when the run has a `not_run (reason:
   model_unavailable)` row or `summary_json["explorer"]` is `{"outcome": "not_run", "reason": "model_unavailable"}`;
   a reproduced finding prints its replayed flow's outcome (the outcome row of its checkpoint).
9. Escape classes: a checkpoint compared in the RC run (`COMPARED`) that did not flag the bug is `covered_missed`; one
   with no comparison there (not in scope, `not_run`, a blocking outcome or `needs_baseline`) is `not_run`; no
   checkpoint is `not_registered`; `not_detectable` (2.13) is a human's call, given explicitly. The escape rate
   counts the escapes on a checkpoint that existed and did not flag (`covered_missed` and `not_run`) and, apart, the
   tier-1 `covered_missed` count of the kill criterion (9.11).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from .. import calendar as cal
from ..records import NON_HUMAN, Escape
from ..release_gate import GateResult, RcDeadlines, ReleaseGatePolicy, evaluate, stored_deadlines
from ..store import Store
from ..store.store import Roster
from .inline import plural
from .safety import RESIDUALS

SUMMARY_SIGNOFF = "rc_signoff"
SUMMARY_VERSIONS = "rc_report_versions"
DEFAULT_LINEAGE = "released"                     # 9.8: one lineage, 'released', in phases 1 to 3
COMPARED = ("identical", "noise", "intended", "collateral", "needs_human", "invariant_violation", "assertion_failed")
SOAK_GLOB = "flow/*/soak/c*"
REPORT_KIND = "report"
REPORT_MEDIA = "text/plain"
SECTIONS = (
    ("lineage", "Comparison against the lineage baseline"),
    ("intended", "Intended by Jira epic"),
    ("sweeps", "Size sweeps per gadget and Ladder F step"),
    ("soak", "Soak"),
    ("explorer", "Explorer"),
    ("migration", "Tier B workspace migration"),
    ("baselines", "Baseline approval status"),
    ("escape_seed", "Escape-tracking seed"),
    ("release_gate", "Release gate checklist"),
    ("decisions", "Decisions on this run"),
    ("signoff", "Sign-off"),
)


class RcReportError(ValueError):
    """An RC report operation the run's state does not allow (unknown run, not signed, no persisted text)."""


@dataclass(frozen=True)
class RcInputs:
    """What the RC sections need beyond the store. `policy` resolves the deadlines when none were recorded on the
    run at RC cut (`release_gate.record_deadlines`); without either, the file is taken as missing (N = 3, G-7)."""

    train: str | None = None
    policy: ReleaseGatePolicy | None = None
    lineage: str = DEFAULT_LINEAGE
    manifest: Mapping[str, Any] | None = None                   # the RC run's manifest (4.3)
    ticket_export: Mapping[str, str | None] | None = None       # release ticket -> its Jira epic (None: no epic)
    migration_workspaces: frozenset[str] = frozenset()          # the tier B workspaces of the corpus (2.3)
    soak_budgets: Mapping[str, str] = field(default_factory=dict)   # metric -> budget text (soak.yaml, 10.11)
    lineage_soak_run_id: str | None = None                      # the lineage soak of the last released build
    log_signatures_available: bool = True                       # False when the client log was not located


NO_INPUTS = RcInputs()


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class AcceptedFinding:
    """An open finding the release manager accepts at release, with its ticket (9.4, 9.12)."""

    finding_id: str                              # the finding's natural key
    ticket: str | None = None


@dataclass(frozen=True)
class SignOffRefusal:
    code: str
    detail: str


class SignOffRefused(Exception):
    """Sign-off refused; `refusals` names each reason."""

    def __init__(self, refusals: Sequence[SignOffRefusal]):
        self.refusals = tuple(refusals)
        super().__init__("; ".join(f"{r.code}: {r.detail}" for r in self.refusals))

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(r.code for r in self.refusals)


@dataclass(frozen=True)
class SignOff:
    """The sign-off block (9.4): who signed, when, which build and version, what was accepted, the approved set."""

    run_id: str
    rc_build: str | None
    system_owner: str
    release_manager: str
    signed_at: str                               # UTC
    version: int                                 # the report version signed
    lineage: str
    accepted: tuple[tuple[str, str], ...]        # (finding id, ticket)
    approved_set: tuple[tuple[str, int, str], ...]   # (checkpoint id, baseline id, image sha256)
    approved_set_sha256: str
    signoff_due: str                             # the G-N sign-off time (UTC)
    late: bool = False                           # signed after it (choice 3)

    def lines(self) -> list[str]:
        accepted = ", ".join(f"{f} ({t})" for f, t in self.accepted) or "none"
        due = f"due {cal.fmt_ny(_dt(self.signoff_due))}, " + ("signed late" if self.late else "signed on time")
        return [f"System owner: {self.system_owner}", f"Release manager: {self.release_manager}",
                f"Signed at: {cal.fmt_ny(_dt(self.signed_at))} ({due})", f"RC build: {self.rc_build or 'unknown'}",
                f"Report version signed: {self.version}", f"Accepted open findings: {accepted}",
                f"Approved baseline set, the next lineage baseline of {self.lineage}: "
                f"{plural(len(self.approved_set), 'checkpoint')}, sha256 {self.approved_set_sha256}",
                "Known residuals, accepted (6.13):", *(f"  - {r}" for r in RESIDUALS)]

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "rc_build": self.rc_build, "system_owner": self.system_owner,
                "release_manager": self.release_manager, "signed_at": self.signed_at, "version": self.version,
                "lineage": self.lineage, "accepted": [list(a) for a in self.accepted],
                "approved_set": [list(a) for a in self.approved_set], "approved_set_sha256": self.approved_set_sha256,
                "signoff_due": self.signoff_due, "late": self.late}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "SignOff":
        return cls(d["run_id"], d.get("rc_build"), d["system_owner"], d["release_manager"], d["signed_at"],
                   int(d["version"]), d["lineage"], tuple((str(a), str(b)) for a, b in d.get("accepted") or ()),
                   tuple((str(c), int(b), str(s)) for c, b, s in d.get("approved_set") or ()),
                   d["approved_set_sha256"], d["signoff_due"], bool(d.get("late")))


@dataclass(frozen=True)
class FrozenVersion:
    """One frozen version of the RC report: its number, the sha256 of its text and of its content, when and why."""

    version: int
    sha256: str
    content_sha256: str
    frozen_at: str
    reason: str
    persisted: bool = False                      # the text was stored as an artifact

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "sha256": self.sha256, "content_sha256": self.content_sha256,
                "frozen_at": self.frozen_at, "reason": self.reason, "persisted": self.persisted}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "FrozenVersion":
        return cls(int(d["version"]), d["sha256"], d["content_sha256"], d["frozen_at"], d["reason"],
                   bool(d.get("persisted")))


@dataclass(frozen=True)
class RcReport:
    """The RC report's header and its RC sections (9.4)."""

    run_id: str
    version: int | None                          # None: live, not frozen
    header: tuple[str, ...]
    sections: tuple[Section, ...]
    gate: GateResult
    superseded_by: str | None = None
    signoff: SignOff | None = None
    stamp: str = ""                              # the version line, outside the content hash

    @property
    def signable(self) -> bool:
        return self.superseded_by is None and self.signoff is None

    def section(self, key: str) -> Section:
        return next(s for s in self.sections if s.key == key)

    def content(self) -> str:
        out = list(self.header)
        for s in self.sections:
            out += ["", f"## {s.title}", *s.lines]
        return "\n".join(out) + "\n"

    def text(self) -> str:
        """The report as frozen: the version stamp, then the content."""
        return f"{self.stamp}\n{self.content()}"

    def content_sha256(self) -> str:
        return _sha(self.content())

    def sha256(self) -> str:
        return _sha(self.text())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dt(text: str) -> datetime:
    return cal.to_utc(datetime.fromisoformat(text))


def _now(at: datetime | None) -> datetime:
    return cal.to_utc(at) if at is not None else datetime.now(cal.UTC)


def _iso(at: datetime) -> str:
    return cal.to_utc(at).isoformat(timespec="seconds")


def _said(outcome: str | None, reason: str | None, detail: str | None = None) -> str:
    if outcome is None:
        return "no outcome"
    if reason is None:
        return outcome
    return f"{outcome} (reason: {reason}" + (f":{detail}" if detail else "") + ")"


# -- the run and its deadlines ---------------------------------------------------------------------------------------

def _run(store: Store, run_id: str) -> Mapping[str, Any]:
    rows = store.query(
        "SELECT r.run_id, r.run_type, r.lifecycle, r.outcome, r.abort_reason, r.started_at, r.summary_json, "
        "p.candidate_build_id, p.base_build_id FROM runs r LEFT JOIN build_pairs p ON p.pair_id = r.pair_id "
        "WHERE r.run_id = ?", (run_id,))
    if not rows:
        raise RcReportError(f"unknown run {run_id}")
    return dict(rows[0])


def deadlines_for(store: Store, run_id: str, inputs: RcInputs = NO_INPUTS) -> RcDeadlines:
    """The deadlines recorded on the run at RC cut, else resolved now from `inputs.policy` (a missing file when
    None) with the run's start as the RC cut."""
    kept = stored_deadlines(store, run_id)
    if kept is not None:
        return kept
    run = _run(store, run_id)
    policy = inputs.policy if inputs.policy is not None else ReleaseGatePolicy(present=False)
    return policy.rc_deadlines(inputs.train, cut_at=_dt(run["started_at"]))


# -- sections --------------------------------------------------------------------------------------------------------

def _lineage_lines(store: Store, run: Mapping[str, Any], lineage: str) -> list[str]:
    approved = store.query("SELECT count(*) FROM baselines WHERE lineage = ? AND state = 'approved'", (lineage,))[0][0]
    lines = [f"Base: {run['base_build_id'] or 'unknown'}, the last released build on lineage {lineage}; "
             f"{plural(approved, 'approved baseline')} in the lineage set"]
    rows = store.query("SELECT c.tier, o.outcome, count(*) AS n FROM outcomes o JOIN checkpoints c "
                       "ON c.checkpoint_id = o.checkpoint_id WHERE o.run_id = ? AND o.synthetic = 0 "
                       "GROUP BY c.tier, o.outcome ORDER BY c.tier, o.outcome", (run["run_id"],))
    per: dict[int, list[str]] = defaultdict(list)
    compared: Counter[int] = Counter()
    total: Counter[int] = Counter()
    for r in rows:
        per[r["tier"]].append(f"{r['outcome']} {r['n']}")
        total[r["tier"]] += r["n"]
        compared[r["tier"]] += r["n"] if r["outcome"] in COMPARED else 0
    for tier in sorted(per):
        lines.append(f"Tier {tier}: {compared[tier]} of {total[tier]} checkpoints compared against the lineage; "
                     + ", ".join(per[tier]))
    if not per:
        lines.append("No outcome recorded")
    return lines


def _intended_lines(store: Store, run_id: str, manifest: Mapping[str, Any] | None,
                    export: Mapping[str, str | None] | None) -> list[str]:
    rows = store.query("SELECT o.manifest_entry, c.surface_id FROM outcomes o JOIN checkpoints c "
                       "ON c.checkpoint_id = o.checkpoint_id WHERE o.run_id = ? AND o.synthetic = 0 "
                       "AND o.outcome = 'intended' ORDER BY o.manifest_entry, c.surface_id", (run_id,))
    entries = {str(e.get("entry_id")): [str(t) for t in e.get("tickets") or ()]
               for e in (manifest or {}).get("entries") or () if isinstance(e, Mapping)}
    by_entry: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        by_entry[r["manifest_entry"] or "(no manifest entry recorded)"].append(r["surface_id"])

    def epic(entry: str) -> str:
        if export is None:
            return "ticket export not supplied"
        tickets = [t for t in entries.get(entry, ()) if t in export]
        if not tickets:
            return "not in the ticket export"
        return export[tickets[0]] or "no epic"

    lines = []
    groups: dict[str, list[str]] = defaultdict(list)
    for entry, surfaces in by_entry.items():
        tickets = ", ".join(entries.get(entry, ())) or "tickets unknown"
        groups[epic(entry)].append(f"{entry} ({tickets}): {plural(len(surfaces), 'intended checkpoint')} on "
                                   + ", ".join(sorted(set(surfaces))))
    for name in sorted(groups):
        lines.append(f"Epic {name}:" if export is not None and name not in ("not in the ticket export", "no epic")
                     else f"{name[0].upper()}{name[1:]}:")
        lines += [f"  {x}" for x in groups[name]]
    if not by_entry:
        lines.append("No intended checkpoint")
    if export is None:
        lines.append("Reconciliation against the ticket export: not possible, no ticket export supplied")
    else:
        in_manifest = {t for ts in entries.values() for t in ts}
        missing = sorted(set(export) - in_manifest)
        extra = sorted(in_manifest - set(export))
        lines.append(f"Reconciliation against the ticket export: {plural(len(export), 'ticket')}, "
                     f"{len(set(export) & in_manifest)} with a manifest entry; not in the manifest: "
                     f"{', '.join(missing) or 'none'}; in the manifest, not in the export: "
                     f"{', '.join(extra) or 'none'}")
    return lines


def _step_key(cid: str, w: int, h: int) -> tuple[int, int, int]:
    asc = cid.endswith("-asc")
    return (1, w, h) if asc else (0, -w, -h)    # Ladder F descends, then its ascending pass (2.5.2, SD-04)


def _sweep_lines(store: Store, run_id: str) -> list[str]:
    rows = store.query("SELECT c.checkpoint_id, c.surface_id, c.host, c.size_w, c.size_h, o.outcome, o.reason, "
                       "o.reason_detail FROM outcomes o JOIN checkpoints c ON c.checkpoint_id = o.checkpoint_id "
                       "WHERE o.run_id = ? AND o.synthetic = 0 AND c.kind = 'sweep'", (run_id,))
    gadgets: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for r in rows:
        gadgets[(r["surface_id"], r["host"])].append(r)
    lines = []
    for (surface, host), steps in sorted(gadgets.items()):
        steps.sort(key=lambda r: _step_key(r["checkpoint_id"], r["size_w"], r["size_h"]))
        bad = [r for r in steps if r["outcome"] != "identical"]

        def step(r: Any) -> str:
            asc = " asc" if r["checkpoint_id"].endswith("-asc") else ""
            return f"{r['size_w']}x{r['size_h']}{asc} {_said(r['outcome'], r['reason'], r['reason_detail'])}"
        lines.append(f"{surface} ({host}): {plural(len(steps), 'step')}, {len(bad)} not identical"
                     + (f"; first in ladder order {step(bad[0])}" if bad else ""))
        lines.append("  " + "; ".join(step(r) for r in steps))
    return lines or ["No sweep checkpoint in this run"]


def _curve(store: Store, run_id: str, metric: str) -> tuple[float, float, float, int] | None:
    rows = store.query("SELECT sampled_at, sum(value) FROM soak_samples WHERE run_id = ? AND metric = ? "
                       "GROUP BY sampled_at ORDER BY sampled_at", (run_id, metric))
    if not rows:
        return None
    values = [float(r[1]) for r in rows]
    return values[0], values[-1], max(values), len(values)


def _soak_lines(store: Store, run_id: str, inputs: RcInputs) -> list[str]:
    cycles = store.query("SELECT o.checkpoint_id, o.outcome, o.reason, o.reason_detail FROM outcomes o "
                         "WHERE o.run_id = ? AND o.synthetic = 0 AND o.checkpoint_id GLOB ? ORDER BY o.checkpoint_id",
                         (run_id, SOAK_GLOB))
    if not cycles:
        return ["Soak did not run: no soak cycle checkpoint in this run"]
    breaches = Counter(c["reason_detail"] for c in cycles if c["outcome"] == "assertion_failed")
    lines = [f"{plural(len(cycles), 'cycle')}: " + "; ".join(
        f"{c['checkpoint_id'].rsplit('/', 1)[-1]} {_said(c['outcome'], c['reason'], c['reason_detail'])}"
        for c in cycles),
        "Budget breaches: " + (", ".join(f"{m} {n}" for m, n in sorted(breaches.items())) or "none")]
    restarts = sum(n for m, n in breaches.items() if m in ("soak.relaunches", "soak.freezes"))
    lines.append(f"Restarts: {restarts} cycles with a freeze or relaunch breach")
    metrics = [r[0] for r in store.query("SELECT DISTINCT metric FROM soak_samples WHERE run_id = ? ORDER BY metric",
                                         (run_id,))]
    for m in metrics:
        cur = _curve(store, run_id, m)
        if cur is None:
            continue
        text = f"{m}: candidate first {cur[0]:g}, last {cur[1]:g}, ceiling {cur[2]:g} ({cur[3]} samples)"
        base = _curve(store, inputs.lineage_soak_run_id, m) if inputs.lineage_soak_run_id else None
        text += (f"; lineage first {base[0]:g}, last {base[1]:g}, ceiling {base[2]:g}" if base
                 else "; lineage curve not available")
        if m in inputs.soak_budgets:
            text += f"; budget {inputs.soak_budgets[m]}"
        lines.append(text)
    if not inputs.log_signatures_available:
        lines.append("Log signatures: not available")
    else:
        sigs = store.query("SELECT natural_key, checkpoint_id, created_at FROM findings WHERE run_id = ? "
                           "AND kind = 'new_log_signature' AND synthetic = 0 ORDER BY created_at, natural_key",
                           (run_id,))
        lines.append(f"New log signatures: {len(sigs)}" + (f"; first {sigs[0]['natural_key']} on "
                                                           f"{sigs[0]['checkpoint_id']} at {sigs[0]['created_at']}"
                                                           if sigs else ""))
    return lines


def _explorer_lines(store: Store, run_id: str, summary: Mapping[str, Any]) -> list[str]:
    exp = summary.get("explorer")
    unavailable = (isinstance(exp, Mapping) and exp.get("outcome") == "not_run"
                   and exp.get("reason") == "model_unavailable") or bool(store.query(
                       "SELECT 1 FROM outcomes WHERE run_id = ? AND outcome = 'not_run' "
                       "AND reason = 'model_unavailable'",
                       (run_id,)))
    if unavailable:
        return ["Explorer: not run, model endpoint unavailable"]
    rows = store.query("SELECT f.natural_key, f.checkpoint_id, f.description, f.held, o.outcome, o.reason, "
                       "o.reason_detail FROM findings f LEFT JOIN outcomes o ON o.run_id = f.run_id "
                       "AND o.checkpoint_id = f.checkpoint_id WHERE f.run_id = ? AND f.kind = 'explorer_candidate' "
                       "AND f.synthetic = 0 ORDER BY f.natural_key", (run_id,))
    shown = [r for r in rows if not r["held"]]
    held = [r for r in rows if r["held"]]
    lines = [f"{r['natural_key']}: {r['description']}; replayed flow outcome "
             f"{_said(r['outcome'], r['reason'], r['reason_detail'])}" for r in shown] or ["No reproduced finding"]
    if held:
        lines.append("Explorer: not reproduced")
        lines += [f"  {r['natural_key']}: {r['description']}" for r in held]
    return lines


def _migration_lines(store: Store, run_id: str, workspaces: frozenset[str]) -> list[str]:
    if not workspaces:
        return ["No tier B migration workspaces supplied"]
    marks = ", ".join("?" for _ in workspaces)
    rows = store.query(f"SELECT c.workspace_id, o.checkpoint_id, o.outcome, o.reason, o.reason_detail FROM outcomes o "
                       f"JOIN checkpoints c ON c.checkpoint_id = o.checkpoint_id "
                       f"WHERE o.run_id = ? AND o.synthetic = 0 "
                       f"AND c.workspace_id IN ({marks}) ORDER BY c.workspace_id, o.checkpoint_id",
                       (run_id, *sorted(workspaces)))
    lines = []
    for ws in sorted(workspaces):
        mine = [r for r in rows if r["workspace_id"] == ws]
        bad = [r for r in mine if r["outcome"] != "identical"]
        lines.append(f"{ws}: {plural(len(mine), 'checkpoint')}, {len(bad)} not identical" if mine
                     else f"{ws}: no checkpoint in this run")
        lines += [f"  {r['checkpoint_id']}: {_said(r['outcome'], r['reason'], r['reason_detail'])}" for r in bad]
    return lines


def _baseline_lines(store: Store, run_id: str, lineage: str) -> list[str]:
    counts = {r[0]: r[1] for r in store.query("SELECT state, count(*) FROM baselines WHERE lineage = ? GROUP BY state",
                                              (lineage,))}
    pending = store.query("SELECT c.tier, count(*) FROM baselines b JOIN checkpoints c "
                          "ON c.checkpoint_id = b.checkpoint_id WHERE b.lineage = ? AND b.state = 'candidate' "
                          "GROUP BY c.tier ORDER BY c.tier", (lineage,))
    lines = [f"Lineage {lineage}: {counts.get('approved', 0)} approved, {counts.get('candidate', 0)} candidates "
             "pending" + (" (" + ", ".join(f"tier {t} {n}" for t, n in pending) + ")" if pending else "")
             + f", {counts.get('retired', 0)} retired"]
    tier1 = store.query("SELECT b.baseline_id, b.checkpoint_id, b.source_run_id FROM baselines b JOIN checkpoints c "
                        "ON c.checkpoint_id = b.checkpoint_id WHERE b.lineage = ? AND b.state = 'candidate' "
                        "AND c.tier = 1 ORDER BY b.checkpoint_id", (lineage,))
    lines += [f"Tier 1 pending: {r['checkpoint_id']} (candidate #{r['baseline_id']}"
              + (f" from run {r['source_run_id']})" if r["source_run_id"] else ")") for r in tier1]
    own = {r[0]: r[1] for r in store.query("SELECT state, count(*) FROM baselines WHERE lineage = ? AND "
                                           "source_run_id = ? GROUP BY state", (lineage, run_id))}
    lines.append(f"From this RC run: {sum(own.values())} candidates queued, {own.get('approved', 0)} approved, "
                 f"{own.get('retired', 0)} retired")
    return lines


def _decision_lines(store: Store, run_id: str) -> list[str]:
    rows = store.query(
        "SELECT d.decision_id, d.action, d.verdict, d.checkpoint_id, d.decided_by, d.decided_at, d.ticket_ref, "
        "d.status FROM decisions d WHERE d.run_id = :run "
        "OR d.decision_id IN (SELECT decision_id FROM findings WHERE run_id = :run AND decision_id IS NOT NULL) "
        "OR d.decision_id IN (SELECT decision_id FROM outcomes WHERE run_id = :run AND decision_id IS NOT NULL) "
        "ORDER BY d.decision_id", {"run": run_id})
    return [f"#{r['decision_id']} {r['action']} ({r['verdict']}) on {r['checkpoint_id']} by {r['decided_by']} at "
            f"{r['decided_at']}" + (f", ticket {r['ticket_ref']}" if r["ticket_ref"] else "") + f", {r['status']}"
            for r in rows] or ["No decision on this run"]


# -- escape tracking (10.9 G+1, 9.11) --------------------------------------------------------------------------------

@dataclass(frozen=True)
class SeedEntry:
    """A tier-1 surface not compared in the RC run, and why (the escape-tracking seed of 9.4)."""

    surface_id: str
    why: str


def escape_seed(store: Store, rc_run_id: str) -> tuple[SeedEntry, ...]:
    """Every tier-1 surface of the RC run (its tier-1 checkpoints and tier-1 coverage cells) with no checkpoint
    compared (`COMPARED`), each with the outcomes or cell reasons that left it uncompared."""
    rows = store.query(
        "SELECT c.surface_id, o.outcome, o.reason FROM checkpoints c LEFT JOIN outcomes o ON o.run_id = :run "
        "AND o.checkpoint_id = c.checkpoint_id AND o.synthetic = 0 WHERE c.tier = 1 AND (o.run_id IS NOT NULL OR "
        "c.checkpoint_id IN (SELECT checkpoint_id FROM run_scope WHERE run_id = :run))", {"run": rc_run_id})
    why: dict[str, Counter[str]] = defaultdict(Counter)
    compared: set[str] = set()
    for r in rows:
        if r["outcome"] in COMPARED:
            compared.add(r["surface_id"])
        else:
            why[r["surface_id"]][_said(r["outcome"], r["reason"])] += 1
    for r in store.query("SELECT surface_id, not_run_reason FROM coverage_cells WHERE run_id = ? AND tier = 1 "
                         "AND state = 'not_run'", (rc_run_id,)):
        if r["surface_id"] not in why and r["surface_id"] not in compared:
            why[r["surface_id"]][f"cell not_run ({r['not_run_reason']})"] += 1
    return tuple(SeedEntry(s, ", ".join(f"{k} {n}" for k, n in sorted(c.items())))
                 for s, c in sorted(why.items()) if s not in compared)


def classify_escape(store: Store, rc_run_id: str | None, checkpoint_id: str | None) -> str:
    """The escape class a G+1 bug gets from the signed RC run (choice 9); `not_detectable` is never inferred."""
    if checkpoint_id is None:
        return "not_registered"
    if rc_run_id is None:
        raise RcReportError("an escape on a checkpoint is classified against the RC run; give rc_run_id or the class")
    r = store.query("SELECT outcome FROM outcomes WHERE run_id = ? AND checkpoint_id = ?", (rc_run_id, checkpoint_id))
    return "covered_missed" if r and r[0][0] in COMPARED else "not_run"


def record_escape(store: Store, *, release: str, ticket: str, entered_by: str, checkpoint_id: str | None = None,
                  rc_run_id: str | None = None, escape_class: str | None = None,
                  at: datetime | None = None) -> Escape:
    """Tag a production bug at G+1 with the checkpoint that should have shown it, or `not_registered` (10.9), and
    write its `escapes` row; the surface and tier come from the checkpoint. Returns the row with its id."""
    surface, tier = None, None
    if checkpoint_id is not None:
        row = store.query("SELECT surface_id, tier FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,))
        if not row:
            raise RcReportError(f"unknown checkpoint {checkpoint_id}; an unregistered surface is not_registered")
        surface, tier = row[0]["surface_id"], row[0]["tier"]
    cls = escape_class or classify_escape(store, rc_run_id, checkpoint_id)
    e = Escape(release, ticket, cls, entered_by, _iso(_now(at)), checkpoint_id, surface, tier)
    return replace(e, escape_id=store.write_escape(e))


@dataclass(frozen=True)
class EscapeRate:
    """The escape figures of one release (9.11): every row by class and tier, the escapes on a checkpoint that existed
    and did not flag (`counted`), and the tier-1 `covered_missed` count of the kill criterion."""

    release: str
    total: int
    by_class: Mapping[str, int]
    by_tier: Mapping[int | None, int]
    counted: int
    tier1_covered_missed: int


def escape_rate(store: Store, release: str) -> EscapeRate:
    rows = store.query("SELECT escape_class, tier FROM escapes WHERE release = ?", (release,))
    by_class = Counter(r[0] for r in rows)
    return EscapeRate(release, len(rows), dict(by_class), dict(Counter(r[1] for r in rows)),
                      by_class["covered_missed"] + by_class["not_run"],
                      sum(r[0] == "covered_missed" and r[1] == 1 for r in rows))


def _seed_lines(store: Store, run_id: str) -> list[str]:
    seed = escape_seed(store, run_id)
    head = f"{plural(len(seed), 'tier-1 surface')} not compared in this RC; G+1 escape tracking starts from these"
    return [head, *(f"  {s.surface_id}: {s.why}" for s in seed)]


# -- the report ------------------------------------------------------------------------------------------------------

def signoff_of(store: Store, run_id: str) -> SignOff | None:
    """The run's sign-off block, None before sign-off."""
    d = store.run_summary(run_id).get(SUMMARY_SIGNOFF)
    return SignOff.from_dict(d) if isinstance(d, Mapping) else None


def versions(store: Store, run_id: str) -> tuple[FrozenVersion, ...]:
    """The run's frozen versions, oldest first; empty before sign-off."""
    return tuple(FrozenVersion.from_dict(v) for v in store.run_summary(run_id).get(SUMMARY_VERSIONS) or ())


def _header(run: Mapping[str, Any], dl: RcDeadlines, lineage: str, by: str | None, gate: GateResult) -> list[str]:
    said = _said(run["outcome"], run["abort_reason"]) if run["outcome"] else run["lifecycle"]
    lines = [f"RC report: run {run['run_id']}, RC build {run['candidate_build_id'] or 'unknown'} against "
             f"{run['base_build_id'] or 'unknown'} (lineage {lineage}), run {said}"]
    if by is not None:
        lines.append(f"superseded by {by}: partial report for the release manager and the release channel; it "
                     f"cannot be signed, and its decisions are inherited by the new RC run")
    lines.append(dl.line())
    if dl.header_line():
        lines.append(str(dl.header_line()))
    blocked = [c.key for c in gate.failing]
    lines.append("Release gate: " + ("all G-N conditions pass" if not blocked else "blocked by " + ", ".join(blocked)))
    return lines


def build_rc_report(store: Store, run_id: str, version: int | None = None, *, inputs: RcInputs = NO_INPUTS,
                    now: datetime | None = None, signoff: SignOff | None = None) -> RcReport:
    """The RC report of `run_id` from the store as it is at `now`. `version` stamps it as that frozen version (None:
    the live report); `signoff` is the block to print, by default the run's recorded one."""
    run = _run(store, run_id)
    if run["run_type"] != "rc":
        raise RcReportError(f"run {run_id} is a {run['run_type']} run; the RC report is for RC runs (9.4)")
    at = _now(now)
    dl = deadlines_for(store, run_id, inputs)
    gate = evaluate(store, run_id, now=at, lineage=inputs.lineage, deadlines=dl)
    by = gate.facts.get("superseded_by")
    block = signoff if signoff is not None else signoff_of(store, run_id)
    summary = json.loads(run["summary_json"] or "{}")
    if by is not None:
        sign = [f"Cannot be signed: superseded by {by}"]
    elif block is not None:
        sign = block.lines()
    else:
        sign = [f"Not signed; sign-off due {cal.fmt_ny(dl.signoff_at)}; release gate "
                + ("passes" if gate.passed else "blocked")]
    bodies = {
        "lineage": _lineage_lines(store, run, inputs.lineage),
        "intended": _intended_lines(store, run_id, inputs.manifest, inputs.ticket_export),
        "sweeps": _sweep_lines(store, run_id),
        "soak": _soak_lines(store, run_id, inputs),
        "explorer": _explorer_lines(store, run_id, summary),
        "migration": _migration_lines(store, run_id, inputs.migration_workspaces),
        "baselines": _baseline_lines(store, run_id, inputs.lineage),
        "escape_seed": _seed_lines(store, run_id),
        "release_gate": gate.checklist(),
        "decisions": _decision_lines(store, run_id),
        "signoff": sign,
    }
    sections = tuple(Section(k, t, tuple(bodies[k])) for k, t in SECTIONS)
    if version is None:
        stamp = f"Live report at {cal.fmt_ny(at)}, not frozen"
    else:
        stamp = f"Version {version}, frozen {cal.fmt_ny(at)}" + (f"; version signed: {block.version}"
                                                                 if block is not None else "")
    return RcReport(run_id, version, tuple(_header(run, dl, inputs.lineage, by, gate)), sections, gate, by, block,
                    stamp)


# -- freezing (9.4) ---------------------------------------------------------------------------------------------------

class ReportSink(Protocol):
    """Where frozen text goes: `store.artifacts.ArtifactStore` for the run's set (`runs/<run_id>`)."""

    set_path: str

    def put(self, data: bytes, *, name: str, kind: str, media_type: str) -> Any: ...

    def get(self, sha: str) -> bytes: ...


def _persist(store: Store, report: RcReport, artifacts: ReportSink | None, at: str) -> bool:
    if artifacts is None:
        return False
    data = report.text().encode("utf-8")
    sha = report.sha256()
    artifacts.put(data, name=f"rc-report-v{report.version}.txt", kind=REPORT_KIND, media_type=REPORT_MEDIA)
    store.insert("artifacts", {"sha256": sha, "kind": REPORT_KIND, "media_type": REPORT_MEDIA, "bytes": len(data),
                               "redaction_passed": 1, "set_path": artifacts.set_path, "first_run_id": report.run_id,
                               "created_at": at, "pinned": 1}, upsert="(sha256) DO NOTHING")
    store.write_artifact_set(sha, artifacts.set_path, created_at=at)
    return True


def _frozen(report: RcReport, at: str, reason: str, persisted: bool) -> FrozenVersion:
    return FrozenVersion(int(report.version or 0), report.sha256(), report.content_sha256(), at, reason, persisted)


def frozen_text(store: Store, run_id: str, version: int, artifacts: ReportSink) -> str:
    """The text of frozen `version`, read back from the artifact set and checked against its recorded sha256."""
    v = next((x for x in versions(store, run_id) if x.version == version), None)
    if v is None:
        raise RcReportError(f"run {run_id} has no frozen version {version}")
    if not v.persisted:
        raise RcReportError(f"version {version} of run {run_id} was frozen without an artifact store")
    data = artifacts.get(v.sha256)
    if _sha(data.decode("utf-8")) != v.sha256:
        raise RcReportError(f"version {version} of run {run_id} does not match its recorded sha256")
    return data.decode("utf-8")


def new_version(store: Store, run_id: str, *, reason: str = "later decision", inputs: RcInputs = NO_INPUTS,
                now: datetime | None = None, artifacts: ReportSink | None = None) -> FrozenVersion | None:
    """Freeze the next version after sign-off when the report's content changed (choice 4); None when unchanged."""
    block = signoff_of(store, run_id)
    if block is None:
        raise RcReportError(f"run {run_id} is not signed; the RC report is frozen at sign-off (9.4)")
    done = versions(store, run_id)
    at = _now(now)
    report = build_rc_report(store, run_id, done[-1].version + 1, inputs=inputs, now=at)
    if report.content_sha256() == done[-1].content_sha256:
        return None
    v = _frozen(report, _iso(at), reason, _persist(store, report, artifacts, _iso(at)))
    store.update_summary(run_id, {SUMMARY_VERSIONS: [*(x.to_dict() for x in done), v.to_dict()]})
    return v


# -- sign-off (9.4, 10.9, 9.12) ---------------------------------------------------------------------------------------

def _people(system_owner: str, release_manager: str, roster: Roster | None) -> list[SignOffRefusal]:
    out = []
    for role, who in (("system owner", system_owner), ("release manager", release_manager)):
        if not isinstance(who, str) or who.strip().lower() in NON_HUMAN:
            out.append(SignOffRefusal("not_a_person", f"the {role} {who!r} is not a person (L-DEC-01)"))
    if not out and system_owner == release_manager:
        out.append(SignOffRefusal("same_person", f"{system_owner} cannot sign as both system owner and release "
                                                 "manager"))
    if roster is not None:
        for key, who in (("system_owner", system_owner), ("release_manager", release_manager)):
            if who not in roster.roles.get(key, frozenset()):
                out.append(SignOffRefusal("not_role_holder", f"{who} does not hold {key} in ops/oncall.yaml"))
    return out


def _accepted(store: Store, run_id: str, accepted: Iterable[AcceptedFinding]
              ) -> tuple[list[tuple[str, str]], list[SignOffRefusal]]:
    ok, bad = [], []
    for a in accepted:
        rows = store.query("SELECT f.ticket_ref, d.ticket_ref FROM findings f LEFT JOIN decisions d "
                           "ON d.decision_id = f.decision_id WHERE f.run_id = ? AND f.natural_key = ?",
                           (run_id, a.finding_id))
        if not rows:
            bad.append(SignOffRefusal("accepted_unknown", f"{a.finding_id} is not a finding of RC run {run_id}"))
            continue
        ticket = a.ticket or rows[0][0] or rows[0][1]
        if not ticket:
            bad.append(SignOffRefusal("accepted_without_ticket", f"accepted finding {a.finding_id} has no ticket"))
        else:
            ok.append((a.finding_id, str(ticket)))
    return ok, bad


def approved_set(store: Store, lineage: str) -> tuple[tuple[str, int, str], ...]:
    """The lineage's approved baselines (checkpoint, baseline id, image sha256): the set a sign-off records (9.8)."""
    return tuple((r[0], int(r[1]), r[2]) for r in store.query(
        "SELECT checkpoint_id, baseline_id, image_sha256 FROM baselines WHERE lineage = ? AND state = 'approved' "
        "ORDER BY checkpoint_id", (lineage,)))


def sign_off(store: Store, run_id: str, system_owner: str, release_manager: str,
             accepted: Sequence[AcceptedFinding] = (), *, inputs: RcInputs = NO_INPUTS, now: datetime | None = None,
             roster: Roster | None = None, artifacts: ReportSink | None = None) -> SignOff:
    """Sign the RC report of `run_id` (9.4, 10.9 G-N) or raise `SignOffRefused` naming every reason. On success the
    block is recorded and version 1 frozen (persisted to `artifacts` when given)."""
    run = _run(store, run_id)
    if run["run_type"] != "rc":
        raise RcReportError(f"run {run_id} is a {run['run_type']} run; only an RC run is signed (9.4)")
    at = _now(now)
    refusals: list[SignOffRefusal] = []
    if signoff_of(store, run_id) is not None:
        refusals.append(SignOffRefusal("already_signed", f"RC run {run_id} is signed; a respin gets a new RC run "
                                                         "and a new sign-off (10.9)"))
    refusals += _people(system_owner, release_manager, roster)
    acc, bad = _accepted(store, run_id, accepted)
    refusals += bad
    dl = deadlines_for(store, run_id, inputs)
    gate = evaluate(store, run_id, now=at, lineage=inputs.lineage, deadlines=dl)
    for c in gate.failing:
        code = "superseded" if c.key == "run" and gate.facts.get("superseded_by") else c.key
        refusals.append(SignOffRefusal(code, "; ".join(c.evidence) or c.title))
    if refusals:
        raise SignOffRefused(refusals)
    approved = approved_set(store, inputs.lineage)
    canon = json.dumps([list(a) for a in approved], separators=(",", ":"))
    block = SignOff(run_id, run["candidate_build_id"], system_owner, release_manager, _iso(at), 1, inputs.lineage,
                    tuple(acc), approved, _sha(canon), _iso(dl.signoff_at), at > dl.signoff_at)
    report = build_rc_report(store, run_id, 1, inputs=inputs, now=at, signoff=block)
    v = _frozen(report, _iso(at), "sign-off", _persist(store, report, artifacts, _iso(at)))
    store.update_summary(run_id, {SUMMARY_SIGNOFF: block.to_dict(), SUMMARY_VERSIONS: [v.to_dict()]})
    return block


# -- lineage candidates (SD-86, 9.8) ---------------------------------------------------------------------------------

def queue_lineage_candidates(store: Store, rc_run_id: str, *, lineage: str = DEFAULT_LINEAGE,
                             now: datetime | None = None) -> tuple[int, ...]:
    """Queue the RC run's `needs_baseline` and `intended` captures as candidates of the lineage set (choice 6);
    returns the new baseline ids. Calling it again queues nothing new."""
    run = _run(store, rc_run_id)
    if run["run_type"] != "rc" or not run["candidate_build_id"]:
        raise RcReportError(f"run {rc_run_id} is not an RC run with a build pair (SD-86)")
    rows = store.query(
        "SELECT o.checkpoint_id, o.cand_capture_sha256 FROM outcomes o WHERE o.run_id = ? AND o.synthetic = 0 "
        "AND o.outcome IN ('needs_baseline', 'intended') AND o.cand_capture_sha256 IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM baselines b WHERE b.checkpoint_id = o.checkpoint_id AND b.lineage = ? "
        "AND b.image_sha256 = o.cand_capture_sha256 AND b.state IN ('candidate', 'approved')) "
        "ORDER BY o.checkpoint_id", (rc_run_id, lineage))
    at = _iso(_now(now))
    return tuple(store.insert("baselines", {"checkpoint_id": r[0], "lineage": lineage,
                                            "build_id": run["candidate_build_id"], "image_sha256": r[1],
                                            "state": "candidate", "source_run_id": rc_run_id, "proposed_at": at})
                 for r in rows)
