"""Registry validation against the running app (2.2.3, 1.4 G1, 4.5) and its Nightly self-test (10.3).

At entry to `running`, right after `canary_flow_passed`, on both builds and in the shard's first tier A workspace
(SD-15), the Orchestrator runs probes P1 to P8. Each probe OCRs a declared list; this module takes the two lists a
probe read (`ProbeListing`, one per build) and decides everything 2.2.3 derives from them, without touching a rig:

- `normalize_label` folds a label for comparison (case fold, collapsed whitespace, hotkey underscores and a trailing
  count such as "(3)" stripped); `label_distance` is the normalized edit distance of two labels.
- `classify_windows(windows, registry)` sorts one build's probe listings into labels that name a registry entry
  (title or alias, among the entries whose `enumerable_by` is the probe) and labels that name none. It is the pure
  classifier both `validate` and the self-test run.
- `pair_renames(cand, base, entries)` applies the 2.2.3 pairing rule: an unmatched candidate label L and an
  unmatched entry E (on the base, absent on the candidate) pair when the normalized edit distance is at most 0.3 or
  their list positions are equal. `substitute_label` puts L in E's OCR rung for E's census visit.
- `validate(probe, cand, base, registry, entitlements, manifest, ...)` gives one probe's `ProbeResult`: the outcome
  of its census checkpoint `census/registry.validation-p<n>/<workspace>`, the `unregistered_surface` records against
  `unregistered.<probe>.<slug>`, the entries whose census checkpoints take 3.11 input B `removed` or whose
  checkpoints take `absent (new_surface)`, the `not_run (reason: entitlement)` and `harness_error (reason:
  registry_entry_not_found)` subsets (selector `surface:<id>`), the registry stub and alias PR payloads, and the run
  health line "validation incomplete for P<n>". `validate_all` runs the eight probes into a `ValidationReport`,
  whose `control()` is the `ControlResult` the Orchestrator applies and whose `maintenance()` gives the entry
  owners' tasks.
- `self_test(registry, probe_lists, surface, ...)` is the Nightly self-test of 10.3: the same validation against a
  registry copy with one surface removed must yield exactly one `unregistered_surface`, else `harness_error
  (reason: validation_self_test)` and the morning banner "registry validation is blind".
- `handles` and `select_subset` resolve the `surface:<id>` selector this module's subsets use.

Choices where the spec is ambiguous (each is reported):

1. Normalization (2.2.3) also applies NFKC, as the locator ladder compares OCR text (5.5). Every underscore is a
   hotkey underscore; one trailing parenthesized count is stripped.
2. Matching is by set: a label names every entry of the probe whose normalized title or alias equals it, an entry
   is on a build when any of its names is in that build's list, and a label repeated in one list counts once (its
   first position is its position).
3. Pairing (2.2.3) is one to one. The edit-distance criterion is applied first, closest pair first (ties by
   candidate position, then entry id), then the position criterion for what is left, since a label inserted before
   a renamed one shifts positions while the rename keeps its spelling close. E's label is the label that named E on
   the base. A pair keeps its `unregistered_surface` record; `rename_candidate: <E.id>` goes in the record's
   `detail`, because 4.13 and 8.3 store the reason as the code `probe` with the probe id as its only parameter.
4. A probe's outcome follows the 1.10 order: `harness_error (reason: ocr_calibration)` when the calibration panel
   failed at `canary_flow_passed`, then `not_run (reason: entitlement)` when the probe's host (the probe surface and
   its parent) needs an entitlement the login lacks, then `landmark_missing (reason: probe:<Pn>)` with the side when
   the anchor was not found on a build, else the comparison. Each of those three ends the probe's validation, so
   the registry stays truth for its area; `harness_error` and `landmark_missing` print "validation incomplete for
   P<n>" (the two 2.2.3 names), `not_run` prints nothing. `identical` compares the normalized lists in order.
5. "The row's parent surface" (2.2.3) for a probe whose lists differ: each differing label (in one list's multiset
   and not the other's; for a pure reorder, each label whose position changed) has as row parent the `parent` of
   every entry it names, or, naming none, the probe surface's `parent`. The outcome is `intended` only when every
   differing label has a row parent the manifest declares, with the declaring entry as `manifest_entry_ref`;
   otherwise, or without a manifest, `collateral`. The record carries a rule classification and the two probe
   screenshots as artifacts.
6. A label on the base only that names no entry produces nothing (2.2.3's table has no row for it).
7. Stub PRs: `registry/surfaces/unregistered/<synthetic id>.yaml` with title, the probe's usual kind (P2 sub-tab,
   P6 ticket variant: the human corrects it), tier 1, `enumerable_by` and the probe surface's parent and owner when
   registered. Alias PRs carry E's id and its alias list with L appended.
8. Alerts (9.10 has no row): `registry_entry_not_found` follows the `harness_error` row plus the system owner (4.5
   "posted to the system owner"); `unregistered_surface` alone notifies the system owner at P3 (1.7 "the system
   owner for new entries"; the Reporter's SENT task carries the one-day SLA of 11.x).
9. The self-test (10.3, G1) counts the `unregistered_surface` records the removal adds: the copy's minus the intact
   registry's, so a surface that is already unregistered on the main build does not blind it. It passes when the
   removal adds exactly one, removes none, and the added one is the removed surface's label. Without a designated
   surface it removes the first entry (by id) a complete probe lists on both builds under a label no other entry
   of that probe carries. A failure lands on the census checkpoint of the removed surface's probe (P1 when no
   surface qualifies).
10. Synthetic slugs: the normalized label in ASCII (accents dropped), runs of other characters as `-`; an empty
    slug, or one another label of the same probe already took, gets the first 8 hex digits of the label's sha256.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .evaluation import BInput
from .orchestrator.alerts import P3, AlertRoute
from .orchestrator.alerts import harness_error as harness_error_route
from .orchestrator.results import ControlResult, FindingSpec, Outcome, SubsetOutcome
from .records import CheckpointOutcome, Classification, MaintenanceItem
from .scope import Manifest, PlannedCheckpoint

PROBES = tuple(f"P{n}" for n in range(1, 9))
PROBE_ROWS = {                                   # 2.2.3 "Validates"
    "P1": "top-level tabs", "P2": "sub-tabs and sections", "P3": "sidebar gadgets", "P4": "Setup entries",
    "P5": "settings pages", "P6": "ticket variants and chain layouts", "P7": "context menus",
    "P8": "Flexible Grid cell contents and detach controls",
}
STUB_KINDS = {"P1": "tab", "P2": "subtab", "P3": "gadget", "P4": "menu", "P5": "settings_page",
              "P6": "ticket_variant", "P7": "context_menu", "P8": "gadget"}       # choice 7
RENAME_DISTANCE_MAX = 0.3                        # 2.2.3 pairing rule
SURFACE_PREFIX = "surface:"                      # SubsetOutcome selector: every checkpoint of one surface
GATE = "running"                                 # SD-15: at entry to running, after canary_flow_passed
MAINTENANCE_KIND = "registry_entry_not_found"    # 11.5 maintenance kind of the entry owner's task
_TRAILING_COUNT = re.compile(r"\s*\(\s*\d+\s*\)\s*$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------------------------
# labels
# --------------------------------------------------------------------------------------------

def normalize_label(label: str) -> str:
    """2.2.3: case fold, collapse whitespace, strip hotkey underscores and a trailing count such as "(3)"; NFKC
    first (choice 1). `normalize_label("_Trade (3)") == "trade"`."""
    text = unicodedata.normalize("NFKC", label).replace("_", "")
    text = _TRAILING_COUNT.sub("", text)
    return " ".join(text.casefold().split())


def label_distance(a: str, b: str) -> float:
    """The normalized edit distance of two labels (2.2.3): Levenshtein distance of their normalized forms over the
    longer length; 0.0 for two empty labels."""
    x, y = normalize_label(a), normalize_label(b)
    if not x and not y:
        return 0.0
    prev = list(range(len(y) + 1))
    for i, cx in enumerate(x, 1):
        cur = [i]
        for j, cy in enumerate(y, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (cx != cy)))
        prev = cur
    return prev[-1] / max(len(x), len(y))


def probe_surface_id(probe: str) -> str:
    """The registered surface of a probe (2.2.3): `registry.validation-p<n>`."""
    _check_probe(probe)
    return f"registry.validation-{probe.lower()}"


def probe_checkpoint_id(probe: str, workspace_id: str) -> str:
    """The probe's census checkpoint (2.2.3): `census/registry.validation-p<n>/<workspace-id>`."""
    return f"census/{probe_surface_id(probe)}/{workspace_id}"


def _check_probe(probe: str) -> None:
    if probe not in PROBES:
        raise ValueError(f"probe {probe!r} is not one of {PROBES} (2.2.3)")


def _slug(normalized: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG.sub("-", ascii_text.lower()).strip("-")


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------------------------------
# registry entries
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    """The fields of a Surface Registry entry (4.5) registry validation reads."""

    id: str
    title: str = ""
    aliases: tuple[str, ...] = ()
    enumerable_by: str = "none"
    entitlements: frozenset[str] = frozenset()
    owner: str | None = None
    parent: str | None = None
    tier: int = 3
    data_mode: str = "frozen"

    @classmethod
    def from_doc(cls, doc: Mapping[str, Any]) -> Entry:
        return cls(id=str(doc["id"]), title=str(doc.get("title") or ""),
                   aliases=tuple(str(a) for a in doc.get("aliases") or ()),
                   enumerable_by=str(doc.get("enumerable_by") or "none"),
                   entitlements=frozenset(doc.get("entitlements") or ()), owner=doc.get("owner"),
                   parent=doc.get("parent"), tier=int(doc.get("tier", 3)),
                   data_mode=str(doc.get("data_mode") or "frozen"))

    @property
    def names(self) -> frozenset[str]:
        """The normalized title and aliases (2.2.1: "used by validation")."""
        return frozenset(n for n in (normalize_label(t) for t in (self.title, *self.aliases)) if n)


Registry = Mapping[str, Mapping[str, Any]]       # surface id -> registry entry document (`SentinelConfig.surfaces`)


def _entries(registry: Registry, probe: str) -> tuple[Entry, ...]:
    """The entries whose `enumerable_by` is the probe, by id."""
    return tuple(Entry.from_doc(registry[k]) for k in sorted(registry)
                 if str(registry[k].get("enumerable_by") or "none") == probe)


def _index(entries: Iterable[Entry]) -> dict[str, tuple[str, ...]]:
    """Normalized name -> the ids of the entries carrying it."""
    out: dict[str, list[str]] = {}
    for e in entries:
        for n in sorted(e.names):
            out.setdefault(n, []).append(e.id)
    return {n: tuple(ids) for n, ids in out.items()}


def _positions(labels: Sequence[str]) -> dict[str, tuple[int, str]]:
    """Normalized label -> (first position, the label as read), list order (choice 2)."""
    out: dict[str, tuple[int, str]] = {}
    for i, raw in enumerate(labels):
        n = normalize_label(raw)
        if n and n not in out:
            out[n] = (i, raw)
    return out


# --------------------------------------------------------------------------------------------
# classification of one build's listings
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeClasses:
    """One probe's list on one build, sorted against the registry (choice 2).

    `registered`: each label (as first read) that names entries, with their ids, list order. `unregistered`: each
    label that names none, list order."""

    registered: tuple[tuple[str, tuple[str, ...]], ...] = ()
    unregistered: tuple[str, ...] = ()

    @property
    def present(self) -> frozenset[str]:
        """The ids of the entries this list names."""
        return frozenset(i for _, ids in self.registered for i in ids)


def classify_windows(windows: Mapping[str, Sequence[str]], registry: Registry) -> dict[str, ProbeClasses]:
    """Sort one build's probe listings (`windows`: probe -> labels as read) against the entries each probe
    enumerates (2.2.3); the pure classifier `validate` and the self-test of 10.3 run."""
    out: dict[str, ProbeClasses] = {}
    for probe, labels in windows.items():
        _check_probe(probe)
        index = _index(_entries(registry, probe))
        reg: list[tuple[str, tuple[str, ...]]] = []
        unreg: list[str] = []
        for n, (_, raw) in sorted(_positions(labels).items(), key=lambda kv: kv[1][0]):
            if n in index:
                reg.append((raw, index[n]))
            else:
                unreg.append(raw)
        out[probe] = ProbeClasses(tuple(reg), tuple(unreg))
    return out


# --------------------------------------------------------------------------------------------
# rename pairing
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RenamePair:
    """A rename candidate (2.2.3): candidate label L, entry E and E's label on the base. `by` is the criterion that
    paired them (`edit_distance` or `position`); `old_names` are E's normalized title and aliases."""

    label: str
    entry_id: str
    base_label: str
    by: str
    distance: float
    old_names: frozenset[str] = frozenset()


def pair_renames(cand: Sequence[str], base: Sequence[str], entries: Iterable[Entry | Mapping[str, Any]]
                 ) -> tuple[RenamePair, ...]:
    """The 2.2.3 pairing of unmatched candidate labels with unmatched entries of one probe (choice 3). `entries`
    are the entries whose `enumerable_by` is the probe."""
    ents = [e if isinstance(e, Entry) else Entry.from_doc(e) for e in entries]
    index = _index(ents)
    cpos, bpos = _positions(cand), _positions(base)
    loose = sorted(((pos, raw) for n, (pos, raw) in cpos.items() if n not in index))
    on_cand = {i for n in cpos for i in index.get(n, ())}
    gone: list[tuple[Entry, int, str]] = []
    for e in ents:
        hits = sorted(bpos[n] for n in e.names if n in bpos)
        if e.id not in on_cand and hits:
            gone.append((e, hits[0][0], hits[0][1]))
    pairs: list[RenamePair] = []
    used_l: set[int] = set()
    used_e: set[str] = set()
    close = sorted((label_distance(raw, blabel), lpos, e.id, raw, e, blabel)
                   for lpos, raw in loose for e, _, blabel in gone)
    for d, lpos, eid, raw, e, blabel in close:
        if d <= RENAME_DISTANCE_MAX and lpos not in used_l and eid not in used_e:
            used_l.add(lpos)
            used_e.add(eid)
            pairs.append(RenamePair(raw, eid, blabel, "edit_distance", d, e.names))
    for lpos, raw in loose:
        if lpos in used_l:
            continue
        hit = next(((e, blabel) for e, bp, blabel in gone if bp == lpos and e.id not in used_e), None)
        if hit is not None:
            e, blabel = hit
            used_l.add(lpos)
            used_e.add(e.id)
            pairs.append(RenamePair(raw, e.id, blabel, "position", label_distance(raw, blabel), e.names))
    return tuple(sorted(pairs, key=lambda p: cpos[normalize_label(p.label)][0]))


def substitute_label(target: Mapping[str, Any], pairs: Iterable[RenamePair]) -> dict[str, Any]:
    """A 5.5 locator target (`executor.locator.target_from_control`) for E's census visit after a rename candidate
    (2.2.3): the OCR rung's text is L when it names E; the JAB and template rungs are unchanged."""
    out = dict(target)
    ocr = target.get("ocr")
    if isinstance(ocr, Mapping) and isinstance(ocr.get("text"), str):
        n = normalize_label(ocr["text"])
        hit = next((p for p in pairs if n in p.old_names), None)
        if hit is not None:
            out["ocr"] = dict(ocr, text=hit.label)
    return out


# --------------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeListing:
    """What one probe read on one build: the labels in list order, whether its anchor region was located, and the
    sha256 of the probe screenshot (the crops of 2.2.3's removed and unregistered rows)."""

    labels: tuple[str, ...] = ()
    located: bool = True
    screenshot: str | None = None


@dataclass(frozen=True)
class ValidationRecord:
    """One outcome registry validation decides on a checkpoint outside the shard's capture loop: a probe's census
    checkpoint, or an unregistered surface's synthetic checkpoint (2.2.3, 1.10)."""

    checkpoint: PlannedCheckpoint
    outcome: Outcome
    side: str | None = None
    artifacts: Mapping[str, str] = field(default_factory=dict)
    classification: Classification | None = None
    manifest_entry_ref: str | None = None

    def to_outcome(self, run_id: str, **stamps: Any) -> CheckpointOutcome:
        """The 4.13 record; `stamps` are the run's fields (fingerprint_id, data_epoch_id, outcome_id, recorded_at,
        a `detail` naming the Watchdog event of a `harness_error`, ...), which override the record's."""
        cp, o = self.checkpoint, self.outcome
        fields: dict[str, Any] = {
            "run_id": run_id, "checkpoint_id": cp.checkpoint_id, "outcome": o.outcome, "reason": o.reason,
            "detail": o.detail, "gate": o.gate, "side": self.side, "workspace_id": cp.workspace_id,
            "persona": cp.persona, "data_mode": cp.data_mode, "surface_id": cp.surface_id, "test_kind": cp.test_kind,
            "tier": cp.tier, "artifacts": dict(self.artifacts) or None, "classification": self.classification,
            "manifest_entry_ref": self.manifest_entry_ref}
        fields.update(stamps)
        return CheckpointOutcome(**fields)


@dataclass(frozen=True)
class Unregistered:
    """A label on the candidate that names no entry (2.2.3 first row): its `unregistered_surface` record against
    `unregistered.<probe>.<slug>`, tier 1, and the entry it may rename."""

    probe: str
    label: str
    record: ValidationRecord
    rename_candidate: str | None = None

    @property
    def surface_id(self) -> str:
        return self.record.checkpoint.surface_id

    def finding(self) -> FindingSpec:
        """The `unregistered_surface` finding the record carries (4.13: the finding is the outcome); the caller
        attaches the record's checkpoint id and screenshots when it gives the finding an id."""
        what = (f"may rename {self.rename_candidate}; an alias PR is proposed" if self.rename_candidate
                else "a registry stub PR is proposed")
        return FindingSpec("unregistered_surface",
                           f"{self.probe} lists \"{self.label}\", which is not in the Surface Registry; {what} "
                           "(2.2.3)", tier=1, detail=self.record.outcome.detail)


@dataclass(frozen=True)
class RegistryProposal:
    """A registry PR payload (2.2.3): `stub` adds an entry for an unregistered label at `path`; `alias` adds the
    label to the aliases of the entry it may rename (`document` is the entry's id and new alias list)."""

    kind: str
    probe: str
    label: str
    surface_id: str
    document: Mapping[str, Any]
    path: str | None = None
    screenshots: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeResult:
    """Everything one probe decides (2.2.3; module docstring).

    `record`: the probe's census checkpoint outcome. `unregistered` and `renames`: the first row and its pairs.
    `removed`: entries on the base, absent and unpaired on the candidate, whose census checkpoints take input B
    `removed` (3.11 rows 24 to 26). `appeared`: entries on the candidate only, whose checkpoints take `needs_baseline`
    (`absent (new_surface)`). `subsets`: `not_run (reason: entitlement)` and `harness_error (reason:
    registry_entry_not_found)` on every checkpoint of an entry absent on both builds. `missing`: the entries of the
    latter, for their owners' tasks."""

    probe: str
    record: ValidationRecord
    unregistered: tuple[Unregistered, ...] = ()
    renames: tuple[RenamePair, ...] = ()
    removed: tuple[str, ...] = ()
    appeared: tuple[str, ...] = ()
    subsets: tuple[SubsetOutcome, ...] = ()
    missing: tuple[Entry, ...] = ()
    proposals: tuple[RegistryProposal, ...] = ()

    @property
    def complete(self) -> bool:
        """False when the registry stays truth for this probe's area (choice 4)."""
        return self.record.outcome.outcome in ("identical", "intended", "collateral")

    @property
    def health(self) -> str | None:
        """The run-health line of a probe that ended `landmark_missing` or `harness_error` (2.2.3, 5.2)."""
        o = self.record.outcome
        if o.outcome not in ("landmark_missing", "harness_error"):
            return None
        side = f" on {self.record.side}" if self.record.side else ""
        return f"validation incomplete for {self.probe}: {o.outcome} ({o.reason}){side}"


class _Checkpoint(Protocol):
    """What `b_input` and `select_subset` read of a checkpoint (`scope.PlannedCheckpoint` has it)."""

    @property
    def surface_id(self) -> str: ...

    @property
    def test_kind(self) -> str: ...


@dataclass(frozen=True)
class ValidationReport:
    """The eight probes of one shard (2.2.3)."""

    probes: tuple[ProbeResult, ...]

    @property
    def records(self) -> tuple[ValidationRecord, ...]:
        """The probes' census checkpoint outcomes, then the `unregistered_surface` records."""
        return tuple(p.record for p in self.probes) + tuple(u.record for u in self.unregistered)

    @property
    def unregistered(self) -> tuple[Unregistered, ...]:
        return tuple(u for p in self.probes for u in p.unregistered)

    @property
    def renames(self) -> tuple[RenamePair, ...]:
        """The rename candidates, for `substitute_label` on each paired entry's census visit."""
        return tuple(r for p in self.probes for r in p.renames)

    @property
    def subsets(self) -> tuple[SubsetOutcome, ...]:
        return tuple(s for p in self.probes for s in p.subsets)

    @property
    def missing(self) -> tuple[Entry, ...]:
        return tuple(e for p in self.probes for e in p.missing)

    @property
    def proposals(self) -> tuple[RegistryProposal, ...]:
        return tuple(x for p in self.probes for x in p.proposals)

    @property
    def health(self) -> tuple[str, ...]:
        """The run-health lines, probe order."""
        return tuple(p.health for p in self.probes if p.health)

    def b_input(self, checkpoint: _Checkpoint) -> BInput | None:
        """Input B registry validation sets for one checkpoint, or None: `removed` on a census checkpoint of a
        removed entry (the census does not attempt its path; the probe screenshots are the crops), `absent
        (new_surface)` on any checkpoint of an entry on the candidate only (2.2.3, 3.11)."""
        sid = checkpoint.surface_id
        if checkpoint.test_kind == "census" and any(sid in p.removed for p in self.probes):
            return BInput("removed")
        if any(sid in p.appeared for p in self.probes):
            return BInput("absent", "new_surface")
        return None

    def control(self) -> ControlResult:
        """The result the Orchestrator applies at entry to `running` (SD-15): the subsets, the 9.10 route (choice
        8) and the entry owners' maintenance tasks; the run continues."""
        missing, unreg = self.missing, self.unregistered
        alert: AlertRoute | None = None
        if missing:
            alert = harness_error_route().plus(notified=("system_owner",))
        elif unreg:
            alert = AlertRoute("unregistered_surface", P3, (), ("system_owner",))
        detail = "; ".join(part for part in (
            f"{len(unreg)} unregistered_surface" if unreg else "",
            f"registry_entry_not_found: {', '.join(e.id for e in missing)}" if missing else "",
            *self.health) if part) or None
        return ControlResult("registry_validation", True, GATE, outcomes=self.subsets, alert=alert,
                             actions=("maintenance_task",) if missing else (), detail=detail)

    def maintenance(self, opened_at: str, run_id: str | None = None) -> tuple[MaintenanceItem, ...]:
        """The maintenance task to each `registry_entry_not_found` entry's owner (2.2.3)."""
        return tuple(MaintenanceItem(kind=MAINTENANCE_KIND, ref=e.id, owner=e.owner or "registry_owner",
                                     opened_at=opened_at, run_id=run_id,
                                     detail=f"{e.id} (\"{e.title}\") is listed by {e.enumerable_by} on neither build "
                                            "while its entitlements are present (2.2.3)")
                     for e in self.missing)


# --------------------------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------------------------

def _probe_checkpoint(probe: str, workspace_id: str, persona: str, host: Entry | None) -> PlannedCheckpoint:
    return PlannedCheckpoint(probe_checkpoint_id(probe, workspace_id), probe_surface_id(probe), "census",
                             host.tier if host else 1, workspace_id, persona, host.data_mode if host else "frozen")


def _shots(cand: ProbeListing, base: ProbeListing) -> dict[str, str]:
    return {k: v for k, v in (("candidate", cand.screenshot), ("base", base.screenshot)) if v}


def _host_entitlements(registry: Registry, host: Entry | None) -> frozenset[str]:
    """The probe surface's entitlements and its parent's (choice 4)."""
    if host is None:
        return frozenset()
    parent = registry.get(host.parent) if host.parent else None
    return host.entitlements | frozenset((parent or {}).get("entitlements") or ())


def _differing(cand: Sequence[str], base: Sequence[str]) -> list[str]:
    """The normalized labels in which two lists differ (choice 5)."""
    c = [n for n in (normalize_label(x) for x in cand) if n]
    b = [n for n in (normalize_label(x) for x in base) if n]
    out = [n for n in dict.fromkeys(c + b) if c.count(n) != b.count(n)]
    if not out:
        out = [n for n in dict.fromkeys(c) if c.index(n) != b.index(n)]
    return out


def _compared(probe: str, cand: ProbeListing, base: ProbeListing, ents: Sequence[Entry], host: Entry | None,
              manifest: Manifest | None) -> tuple[Outcome, Classification | None, str | None]:
    """`identical`, or `intended` / `collateral` by the manifest's declaration of the rows' parents (choice 5)."""
    c = [n for n in (normalize_label(x) for x in cand.labels) if n]
    b = [n for n in (normalize_label(x) for x in base.labels) if n]
    if c == b:
        return Outcome("identical"), None, None
    diff = _differing(cand.labels, base.labels)
    index = {e.id: e for e in ents}
    names = _index(ents)
    declared = manifest.surfaces if manifest is not None else frozenset()
    refs: list[str] = []
    for n in diff:
        parents = {index[i].parent for i in names.get(n, ())} if n in names else {host.parent if host else None}
        hit = sorted(p for p in parents if p and p in declared)
        if not hit:
            refs = []
            break
        entry = manifest.entry_declaring(hit[0]) if manifest is not None else None
        if entry is not None:
            refs.append(entry.entry_id)
    detail = f"{probe} lists differ in {', '.join(repr(n) for n in diff)}"
    if refs:
        return (Outcome("intended", detail=detail), Classification("intended", "rule", description=detail),
                refs[0])
    return Outcome("collateral", detail=detail), Classification("collateral", "rule", description=detail), None


def validate(probe: str, cand: ProbeListing, base: ProbeListing, registry: Registry, entitlements: Iterable[str],
             manifest: Manifest | None, *, workspace_id: str, persona: str, ocr_calibrated: bool = True
             ) -> ProbeResult:
    """One probe's results (2.2.3; module docstring). `registry` maps surface id to entry document, `entitlements`
    is the login's profile, `workspace_id` and `persona` the shard's first tier A workspace and its persona, and
    `ocr_calibrated` False when the calibration panel failed at `canary_flow_passed` (2.8.1)."""
    _check_probe(probe)
    ents = _entries(registry, probe)
    sid = probe_surface_id(probe)
    host = Entry.from_doc(registry[sid]) if sid in registry else None
    cp = _probe_checkpoint(probe, workspace_id, persona, host)
    shots = _shots(cand, base)
    have = frozenset(entitlements)
    rows = PROBE_ROWS[probe]
    if not ocr_calibrated:
        return ProbeResult(probe, ValidationRecord(cp, Outcome(
            "harness_error", "ocr_calibration", detail=f"OCR calibration panel failed at canary_flow_passed; {probe} "
                                                       f"({rows}) not validated"), artifacts=shots))
    lacking = _host_entitlements(registry, host) - have
    if lacking:
        return ProbeResult(probe, ValidationRecord(cp, Outcome(
            "not_run", "entitlement", detail=f"the host of {probe} needs {', '.join(sorted(lacking))}"),
            artifacts=shots))
    lost = [s for s, listing in (("candidate", cand), ("base", base)) if not listing.located]
    if lost:
        side = "both" if len(lost) == 2 else lost[0]
        return ProbeResult(probe, ValidationRecord(cp, Outcome(
            "landmark_missing", f"probe:{probe}", detail=f"anchor of {probe} ({rows}) not found on {' and '.join(lost)}"
                                                         "; the registry stays truth there"),
            side=side, artifacts=shots))
    outcome, cls, ref = _compared(probe, cand, base, ents, host, manifest)
    record = ValidationRecord(cp, outcome, artifacts=shots, classification=cls, manifest_entry_ref=ref)
    classes = classify_windows({probe: cand.labels}, registry)[probe]
    base_classes = classify_windows({probe: base.labels}, registry)[probe]
    pairs = pair_renames(cand.labels, base.labels, ents)
    paired = {p.entry_id for p in pairs}
    removed: list[str] = []
    appeared: list[str] = []
    subsets: list[SubsetOutcome] = []
    missing: list[Entry] = []
    for e in ents:
        on_c, on_b = e.id in classes.present, e.id in base_classes.present
        if on_b and not on_c and e.id not in paired:
            removed.append(e.id)
        elif on_c and not on_b:
            appeared.append(e.id)
        elif not on_c and not on_b:
            if e.entitlements - have:
                subsets.append(SubsetOutcome(SURFACE_PREFIX + e.id, Outcome(
                    "not_run", "entitlement", detail=f"{probe} lists it on neither build; the login lacks "
                                                     f"{', '.join(sorted(e.entitlements - have))}")))
            else:
                missing.append(e)
                subsets.append(SubsetOutcome(SURFACE_PREFIX + e.id, Outcome(
                    "harness_error", "registry_entry_not_found",
                    detail=f"{probe} lists \"{e.title}\" on neither build while its entitlements are present")))
    unregistered, proposals = _unregistered(probe, cand, base, classes, base_classes, pairs, ents, host,
                                            workspace_id, persona, shots)
    return ProbeResult(probe, record, unregistered, pairs, tuple(removed), tuple(appeared), tuple(subsets),
                       tuple(missing), proposals)


def _unregistered(probe: str, cand: ProbeListing, base: ProbeListing, classes: ProbeClasses,
                  base_classes: ProbeClasses, pairs: Sequence[RenamePair], ents: Sequence[Entry], host: Entry | None,
                  workspace_id: str, persona: str, shots: Mapping[str, str]
                  ) -> tuple[tuple[Unregistered, ...], tuple[RegistryProposal, ...]]:
    """The first row of 2.2.3's table: one record and one PR payload per unregistered candidate label."""
    by_label = {normalize_label(p.label): p for p in pairs}
    on_base = {normalize_label(x) for x in base_classes.unregistered}
    entries = {e.id: e for e in ents}
    taken: dict[str, str] = {}
    out: list[Unregistered] = []
    props: list[RegistryProposal] = []
    for raw in classes.unregistered:
        n = normalize_label(raw)
        slug = _slug(n)
        if not slug or taken.get(slug, n) != n:
            slug = f"{slug}-{_sha8(n)}" if slug else f"label-{_sha8(n)}"
        taken[slug] = n
        sid = f"unregistered.{probe.lower()}.{slug}"
        pair = by_label.get(n)
        sides = "candidate and base" if n in on_base else "candidate only"
        detail = f"label \"{raw}\" listed by {probe} ({PROBE_ROWS[probe]}) on {sides} in {workspace_id}"
        if pair is not None:
            detail += f"; rename_candidate: {pair.entry_id} (base label \"{pair.base_label}\", by {pair.by})"
        cp = PlannedCheckpoint(f"census/{sid}/{workspace_id}", sid, "census", 1, workspace_id, persona, "frozen")
        record = ValidationRecord(cp, Outcome("unregistered_surface", f"probe:{probe}", detail=detail),
                                  artifacts=shots)
        out.append(Unregistered(probe, raw, record, pair.entry_id if pair else None))
        if pair is not None:
            e = entries[pair.entry_id]
            props.append(RegistryProposal("alias", probe, raw, e.id, {"id": e.id, "aliases": [*e.aliases, raw]},
                                          screenshots=dict(shots)))
        else:
            doc: dict[str, Any] = {"schema_version": 1, "id": sid, "title": raw, "kind": STUB_KINDS[probe],
                                   "tier": 1, "enumerable_by": probe}
            if host is not None and host.parent:
                doc["parent"] = host.parent
            if host is not None and host.owner:
                doc["owner"] = host.owner
            props.append(RegistryProposal("stub", probe, raw, sid, doc,
                                          path=f"registry/surfaces/unregistered/{sid}.yaml", screenshots=dict(shots)))
    return tuple(out), tuple(props)


def validate_all(listings: Mapping[str, tuple[ProbeListing, ProbeListing]], registry: Registry,
                 entitlements: Iterable[str], manifest: Manifest | None, *, workspace_id: str, persona: str,
                 ocr_calibrated: bool = True) -> ValidationReport:
    """The eight probes (2.2.3): `listings` maps every probe P1 to P8 to its (candidate, base) listings; a probe
    left out raises, since validation never silently accepts the registry."""
    left_out = [p for p in PROBES if p not in listings]
    if left_out:
        raise ValueError(f"no listings for {', '.join(left_out)}: registry validation runs all eight probes (2.2.3)")
    have = frozenset(entitlements)
    return ValidationReport(tuple(
        validate(p, listings[p][0], listings[p][1], registry, have, manifest, workspace_id=workspace_id,
                 persona=persona, ocr_calibrated=ocr_calibrated) for p in PROBES))


# --------------------------------------------------------------------------------------------
# the surface:<id> selector
# --------------------------------------------------------------------------------------------

def handles(selector: str) -> bool:
    """True for the selector this module's subsets use: `surface:<id>`, every checkpoint of one surface."""
    return selector.startswith(SURFACE_PREFIX) and len(selector) > len(SURFACE_PREFIX)


def select_subset(selector: str, checkpoints: Sequence[_Checkpoint]) -> list[Any]:
    """The checkpoints of `checkpoints` a `surface:<id>` selector names, in their order."""
    if not handles(selector):
        raise ValueError(f"selector {selector!r} is not {SURFACE_PREFIX}<id>")
    sid = selector[len(SURFACE_PREFIX):]
    return [cp for cp in checkpoints if cp.surface_id == sid]


# --------------------------------------------------------------------------------------------
# the Nightly self-test (10.3)
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SelfTestResult:
    """The registry validation self-test (10.3, G1). `added`: the synthetic ids the removal added. On failure
    `record` is `harness_error (reason: validation_self_test)` on the probe's census checkpoint and `banner` the
    morning report's line."""

    surface: str | None
    probe: str
    added: tuple[str, ...] = ()
    record: ValidationRecord | None = None
    banner: str | None = None

    @property
    def passed(self) -> bool:
        return self.record is None

    def control(self) -> ControlResult:
        """The result the Nightly applies: on failure the `harness_error` row of 9.10 (P2 to on-call)."""
        if self.record is None:
            return ControlResult("validation_self_test", True, GATE,
                                 detail=f"removed {self.surface}; one unregistered_surface")
        return ControlResult("validation_self_test", False, GATE, alert=harness_error_route(),
                             detail=self.record.outcome.detail)


Validator = Callable[..., ValidationReport]


def _self_test_surface(registry: Registry, probe_lists: Mapping[str, tuple[ProbeListing, ProbeListing]],
                       entitlements: frozenset[str]) -> str | None:
    """The first entry (by id) a complete probe lists on both builds under a label no other entry of that probe
    carries (choice 9)."""
    for sid in sorted(registry):
        e = Entry.from_doc(registry[sid])
        if e.enumerable_by not in probe_lists:
            continue
        host_doc = registry.get(probe_surface_id(e.enumerable_by))
        host = Entry.from_doc(host_doc) if host_doc is not None else None
        cand, base = probe_lists[e.enumerable_by]
        if not (cand.located and base.located) or _host_entitlements(registry, host) - entitlements:
            continue
        index = _index(_entries(registry, e.enumerable_by))
        both = set(_positions(cand.labels)) & set(_positions(base.labels))
        if any(n in both and index.get(n) == (e.id,) for n in e.names):
            return sid
    return None


def self_test(registry: Registry, probe_lists: Mapping[str, tuple[ProbeListing, ProbeListing]],
              surface: str | None = None, *, entitlements: Iterable[str], workspace_id: str, persona: str,
              validator: Validator = validate_all) -> SelfTestResult:
    """Run `validator` (the validation of 2.2.3) on the recorded `probe_lists` against `registry` and against a copy
    without `surface` (default: choice 9); exactly one more `unregistered_surface`, the removed surface's, passes
    (10.3, G1)."""
    have = frozenset(entitlements)
    if surface is None:
        surface = _self_test_surface(registry, probe_lists, have)
    elif surface not in registry:
        raise ValueError(f"surface {surface!r} is not in the registry")
    probe = Entry.from_doc(registry[surface]).enumerable_by if surface is not None else PROBES[0]
    if probe not in PROBES:
        raise ValueError(f"surface {surface!r} is enumerable by no probe ({probe}); the self-test removes one a "
                         "probe lists")
    host_doc = registry.get(probe_surface_id(probe))
    cp = _probe_checkpoint(probe, workspace_id, persona, Entry.from_doc(host_doc) if host_doc is not None else None)

    def failed(detail: str, added: tuple[str, ...] = ()) -> SelfTestResult:
        banner = (f"Registry validation is blind: the Nightly self-test {detail}; the registry probes are blind "
                  "until a Nightly self-test passes (G1, 10.3).")
        return SelfTestResult(surface, probe, added, ValidationRecord(cp, Outcome(
            "harness_error", "validation_self_test", detail=detail)), banner)

    if surface is None:
        return failed("found no registered surface a probe lists on both builds, so it could not remove one")
    entry = Entry.from_doc(registry[surface])
    copy = {k: v for k, v in registry.items() if k != surface}

    def unregistered(reg: Registry) -> dict[str, Unregistered]:
        report = validator(probe_lists, reg, have, None, workspace_id=workspace_id, persona=persona)
        return {u.surface_id: u for u in report.unregistered}

    before, after = unregistered(registry), unregistered(copy)
    added = tuple(sorted(set(after) - set(before)))
    lost = tuple(sorted(set(before) - set(after)))
    if len(added) == 1 and not lost and normalize_label(after[added[0]].label) in entry.names:
        return SelfTestResult(surface, probe, added)
    what = f"{len(added)} unregistered_surface ({', '.join(added) or 'none'})"
    if lost:
        what += f" and lost {', '.join(lost)}"
    return failed(f"removed {surface} ({probe}) from a registry copy and got {what} instead of exactly one, "
                  f"\"{entry.title}\"", added)
