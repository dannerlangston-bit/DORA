"""The lint pipeline modes of 4.15 beyond `--all`, and the rules that need more than one tree, a store or a run.

4.15 "Where the lint runs": `sentinel lint --changed` on a sentinel-config pull request (error blocks, warn becomes a
review comment), `--manifest .sentinel/manifest.yaml` as the product repository's `sentinel/manifest` check
(informational in Phase 1, blocking from Phase 2, 4.3), `--all --config-commit <sha>` at run start (12.7 G1), and
`--file <flow>` for the Recorder and for Explorer candidates (a failing candidate is kept as an `explorer_candidate`
finding with `replayed: lint_failed`, never replayed, 2.9).

History rules (base tree against head tree, `history_violations`):

- L-FLW-03: a flow checkpoint id the base defined and the head does not has a flows/RENAMES.yaml entry (4.7);
- L-SRF-01 (2.2.4 "renamed ids without a rename map"): a surface id the base registry had and the head lacks has a
  surface entry in flows/RENAMES.yaml (`old: <surface id>`, rules.py choice 28), which carries every checkpoint id
  of the surface (`follow_renames`, the re-keying of decisions and calibration rows);
- L-XREF-10: an approved checkpoint whose id disappeared, or whose regions file changed a landmark template (path or
  bytes) or a region `rect`, carries a RENAMES entry or `recalibrate: true` in its head regions file;
- L-MSK-05: a renewal (a later `expires_at` on a mask both trees hold) quotes the latest nightly A/A result for the
  region in `reason`; a region stable in A/A is dropped, not renewed;
- L-MSK-04 (warn): the masked share of a tier-1 checkpoint grew by more than 2 percentage points against the tree
  of 30 days ago.

Run-start and store rules: `store_rules` (L-MSK-01: `decision_ref` is a `request_mask` decision of the Central Store
by a human of the triage roster in ops/oncall.yaml), `run_context_rules` (L-XREF-16, L-MAN-09) and
`lint_manifest_against_base` (L-MAN-10).

Choices where the spec is ambiguous (reported):

1. `--changed`: every error of the merged tree is reported, wherever it is (a change can break an untouched file,
   4.15 cross-file rationale), and warnings only on the changed files and from the history rules; the changed files
   are `git diff --name-only BASE` plus untracked files, relative to the config root.
2. Approved checkpoints (L-XREF-10) come from the caller (a store query or a file); without that list every
   checkpoint with a regions file in the base tree counts as approved, the conservative reading.
3. A renewal "quotes" the A/A result when `reason` contains the run id of the latest nightly A/A result for (checkpoint,
   region); the region is stable when that result's changed-pixel count is 0. Without A/A results the renewal must
   change `reason` and name "A/A".
4. Masked share (L-MSK-04): the union of the checkpoint's mask regions on each landmark over the bounding box of all
   its regions on that landmark, summed over landmarks (rules.py choice 9's geometry).
5. L-MSK-01 reads the roster as the store does (`Roster.from_oncall`: the triage roster plus the triage rotation);
   a missing decision, another action, a non-human decider or one outside the roster is the error.
6. L-XREF-16: the run context names the authorization version (`authorization_ref`: the version, or
   `authorization/<version>.md` or `.yaml`) and its sha256; both files exist in the config tree and the sha256 is the
   document's.
7. The `sentinel/manifest` status: `failure` when the manifest has an error, `blocking` from Phase 2; in the product
   repository the candidate build is not yet in builds/, so that one reference is not checked there.
8. L-MAN-10 counts a ticket as a feature ticket when `ticket_types` (a Jira export) gives its type as `feature`
   (case folded, `story` too); without types only the base registry decides.
"""
from __future__ import annotations

import dataclasses
import io
import json
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol

from ..records import Finding
from .repo import Doc, Repo, _read
from .rules import (
    RULES, LintReport, Violation, _rect, _union_area, checkpoint_definitions, file_sha256, lint_repo, surface_rename,
)
from .runstart import lint_at_run_start

HISTORY_RULES = ("L-XREF-10", "L-MSK-04", "L-MSK-05")
MASK_GROWTH_MAX = 0.02                  # L-MSK-04: 2 percent of a tier-1 checkpoint over 30 days
MASK_GROWTH_DAYS = 30
MANIFEST_CONTEXT = "sentinel/manifest"  # 4.3: the product repository's PR check
MANIFEST_BLOCKING_PHASE = 2             # 4.3: informational in Phase 1, blocking from Phase 2
FEATURE_TICKET_TYPES = frozenset({"feature", "story"})


class LintModeError(RuntimeError):
    """A mode cannot run: not a git checkout, an unknown revision, an unreadable file."""


def _v(rule: str, file: str, message: str, path: str = "", severity: str | None = None) -> Violation:
    return Violation(file, rule, severity or RULES[rule].severity, path, message)


def _d(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x: Any) -> list:
    return x if isinstance(x, list) else []


# -- git trees -----------------------------------------------------------------------------------------------------

def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        err = getattr(exc, "stderr", b"") or b""
        raise LintModeError(f"git {' '.join(args)}: {err.decode(errors='replace').strip() or exc}") from exc


def tree_at(root: str | Path, rev: str, dest: str | Path) -> Path:
    """Extract the config tree at `rev` (the directory `root` names inside its git repository) into `dest`."""
    root, dest = Path(root), Path(dest)
    prefix = _git(root, "rev-parse", "--show-prefix").decode().strip()
    top = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip())     # archive limits itself to the cwd
    data = _git(top, "archive", "--format=tar", f"{rev}:{prefix}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def changed_files(root: str | Path, base_rev: str) -> frozenset[str]:
    """Paths relative to `root` that differ from `base_rev`, untracked files included (choice 1)."""
    root = Path(root)
    diff = _git(root, "diff", "--name-only", "--relative", base_rev).decode().splitlines()
    new = _git(root, "ls-files", "--others", "--exclude-standard").decode().splitlines()
    return frozenset(p.strip() for p in diff + new if p.strip())


def commit_before(root: str | Path, day: date) -> str | None:
    """The last commit of HEAD made on or before `day`, or None."""
    sha = _git(Path(root), "rev-list", "-1", f"--before={day.isoformat()}T23:59:59", "HEAD").decode().strip()
    return sha or None


@dataclass
class _Tree:
    """A repository loaded from a revision's tree, kept on disk while in use."""
    tmp: tempfile.TemporaryDirectory
    repo: Repo


def _load_rev(root: Path, rev: str) -> _Tree:
    tmp = tempfile.TemporaryDirectory(prefix="sentinel-lint-")
    return _Tree(tmp, Repo.load(tree_at(root, rev, tmp.name)))


# -- renames -------------------------------------------------------------------------------------------------------

def renames_of(repo: Repo) -> list[dict]:
    """The entries of flows/RENAMES.yaml."""
    doc = repo.one("renames")
    return [_d(x) for x in _l(_d(doc.data if doc else None).get("renames"))]


def follow_renames(checkpoint_id: str, renames: Iterable[Mapping[str, Any]]) -> str | None:
    """Where a checkpoint id went: the new id of a checkpoint entry, the id with its surface segment replaced by a
    surface entry (rules.py choice 28), `retired`, or None when no entry carries it. The integrator re-keys decisions
    and calibration rows with it (2.2.4: "decisions and calibration rows follow it")."""
    entries = list(renames)
    for x in entries:
        if x.get("old") == checkpoint_id and not surface_rename(x):
            return str(x.get("new"))
    parts = checkpoint_id.split("/")
    for x in entries:
        if surface_rename(x) and len(parts) > 2 and x.get("old") == parts[1]:
            return "retired" if x.get("new") == "retired" else "/".join([parts[0], str(x.get("new")), *parts[2:]])
    return None


# -- history rules -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AAResult:
    """The latest nightly A/A result of one region (10.6): the run and its changed-pixel count between the passes."""
    run_id: str
    changed_px: int


def _regions(repo: Repo) -> dict[str, Doc]:
    return {d.rel[len("regions/"):-len(".yaml")]: d for d in repo.of("regions")}


def _template_hash(repo: Repo, rel: Any) -> str | None:
    return file_sha256(repo.path(rel)) if isinstance(rel, str) and repo.exists(rel) else None


def _changed_geometry(base: Repo, head: Repo, b: Doc, h: Doc) -> list[str]:
    """What changed between two versions of one regions file: landmark templates (path or bytes) and region rects."""
    out = []
    bl = {_d(x).get("id"): _d(x) for x in _l(_d(b.data).get("landmarks"))}
    hl = {_d(x).get("id"): _d(x) for x in _l(_d(h.data).get("landmarks"))}
    for lid, lm in bl.items():
        new = hl.get(lid)
        if new is None or new.get("template") != lm.get("template") or \
                _template_hash(base, lm.get("template")) != _template_hash(head, new.get("template")):
            out.append(f"landmark {lid} template")
    br = {_d(x).get("id"): _d(x).get("rect") for x in _l(_d(b.data).get("regions"))}
    hr = {_d(x).get("id"): _d(x).get("rect") for x in _l(_d(h.data).get("regions"))}
    out += [f"region {rid} rect" for rid, rect in br.items() if rid in hr and hr[rid] != rect]
    out += [f"region {rid} removed" for rid in br if rid not in hr]
    return out


def _masked_share(regions: Iterable[Any]) -> float:
    """Choice 4: masked area over the bounds of all regions, per landmark, summed over landmarks."""
    regs = [_d(x) for x in regions if _rect(_d(x).get("rect"))]
    masked = total = 0
    for lm in sorted({str(x.get("landmark")) for x in regs}):
        rects = [_rect(x["rect"]) for x in regs if str(x.get("landmark")) == lm]
        x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)  # type: ignore[index]
        x1, y1 = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)  # type: ignore[index]
        bounds = (x0, y0, x1 - x0, y1 - y0)
        total += bounds[2] * bounds[3]
        masked += _union_area(bounds, [_rect(x["rect"]) for x in regs  # type: ignore[misc]
                                       if str(x.get("landmark")) == lm and x.get("type") == "mask"])
    return masked / total if total else 0.0


def history_violations(base: Repo, head: Repo, *, approved: Iterable[str] | None = None,
                       aa: Mapping[tuple[str, str], AAResult] | None = None,
                       month_ago: Repo | None = None) -> list[Violation]:
    """The history rules of the module docstring, comparing the base tree with the head tree; `month_ago` is the
    tree of 30 days ago (L-MSK-04), `approved` the approved checkpoint ids (L-XREF-10, choice 2), `aa` the latest
    nightly A/A result per (checkpoint, region) (L-MSK-05, choice 3)."""
    out: list[Violation] = []
    renames = renames_of(head)
    renames_file = head.one("renames").rel if head.one("renames") else "flows/RENAMES.yaml"
    base_def, _ = checkpoint_definitions(base)
    head_def, _ = checkpoint_definitions(head)
    for cid, c in sorted(base_def.items()):
        if c.kind == "flow" and cid not in head_def and follow_renames(cid, renames) is None:
            out.append(_v("L-FLW-03", c.file, f"checkpoint {cid} was removed or renamed without a {renames_file} entry "
                                              "(old, new or retired, build); its baselines would be orphaned"))
    base_surfaces = {_d(d.data).get("id"): d.rel for d in base.of("surface")}
    head_surfaces = {_d(d.data).get("id") for d in head.of("surface")}
    surface_entries = {x.get("old") for x in renames if surface_rename(x)}
    for sid, rel in sorted((k, v) for k, v in base_surfaces.items() if isinstance(k, str)):
        if sid not in head_surfaces and sid not in surface_entries:
            out.append(_v("L-SRF-01", rel, f"surface {sid} was removed or renamed without a rename map entry in "
                                           f"{renames_file} (old: {sid}, new: <id> or retired, build; 2.2.4)", "id"))
    out += _xref10(base, head, renames, renames_file, approved)
    out += _renewals(base, head, aa)
    if month_ago is not None:
        out += _growth(month_ago, head, head_def)
    return sorted(set(out))


def _xref10(base: Repo, head: Repo, renames: list[dict], renames_file: str,
            approved: Iterable[str] | None) -> list[Violation]:
    out = []
    b_reg, h_reg = _regions(base), _regions(head)
    for cid in sorted(set(b_reg) if approved is None else set(approved) & set(b_reg)):
        moved = follow_renames(cid, renames)
        h = h_reg.get(cid)
        if h is None:
            if moved is None:
                out.append(_v("L-XREF-10", b_reg[cid].rel, f"approved checkpoint {cid} disappeared without a "
                                                           f"{renames_file} entry; its baseline would be orphaned"))
            continue
        what = _changed_geometry(base, head, b_reg[cid], h)
        if what and _d(h.data).get("recalibrate") is not True and moved is None:
            out.append(_v("L-XREF-10", h.rel, f"approved checkpoint {cid} changed ({', '.join(what)}) without "
                                              f"recalibrate: true or a {renames_file} entry", "recalibrate"))
    return out


def _masks(repo: Repo) -> dict[tuple[str, str], Doc]:
    return {(str(_d(d.data).get("checkpoint_id")), str(_d(d.data).get("mask_id"))): d for d in repo.of("mask")}


def _when(v: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None
    return t if t is None or t.tzinfo else t.replace(tzinfo=timezone.utc)


def _renewals(base: Repo, head: Repo, aa: Mapping[tuple[str, str], AAResult] | None) -> list[Violation]:
    out = []
    old = _masks(base)
    for key, doc in sorted(_masks(head).items()):
        prev = old.get(key)
        m, p = _d(doc.data), _d(prev.data if prev else None)
        before, after = _when(p.get("expires_at")), _when(m.get("expires_at"))
        if prev is None or before is None or after is None or after <= before:
            continue
        reason, region = str(m.get("reason") or ""), str(m.get("region_id"))
        if aa is None:
            if reason == str(p.get("reason") or "") or "A/A" not in reason:
                out.append(_v("L-MSK-05", doc.rel, "a renewal quotes the latest nightly A/A result for the region in "
                                                   "reason (3.8)", "reason"))
            continue
        res = aa.get((key[0], region))
        if res is None:
            out.append(_v("L-MSK-05", doc.rel, f"no nightly A/A result for {region} to quote; the renewal waits for one",
                          "reason"))
        elif res.changed_px == 0:
            out.append(_v("L-MSK-05", doc.rel, f"{region} is stable in A/A ({res.run_id}); drop the mask instead of "
                                               "renewing it (3.8)", "expires_at"))
        elif res.run_id not in reason:
            out.append(_v("L-MSK-05", doc.rel, f"the renewal's reason does not quote the latest nightly A/A result "
                                               f"{res.run_id} ({res.changed_px} px) for {region}", "reason"))
    return out


def _growth(then: Repo, now: Repo, defined: Mapping[str, Any]) -> list[Violation]:
    out = []
    old = _regions(then)
    for cid, doc in sorted(_regions(now).items()):
        c = defined.get(cid)
        if c is None or c.tier != 1:
            continue
        was = _masked_share(_l(_d(old[cid].data).get("regions"))) if cid in old else 0.0
        grew = _masked_share(_l(_d(doc.data).get("regions"))) - was
        if grew > MASK_GROWTH_MAX:
            out.append(_v("L-MSK-04", doc.rel, f"masked share of tier-1 checkpoint {cid} grew {grew:.1%} in "
                                               f"{MASK_GROWTH_DAYS} days; at most {MASK_GROWTH_MAX:.0%}"))
    return out


# -- --changed -----------------------------------------------------------------------------------------------------

def history_at(root: str | Path, base_rev: str, *, approved: Iterable[str] | None = None,
               aa: Mapping[tuple[str, str], AAResult] | None = None, today: date | None = None) -> list[Violation]:
    """The history rules of the checkout at `root` against its tree at `base_rev`, and against its tree of 30 days
    before `today` (L-MSK-04) when the history reaches that far."""
    root = Path(root)
    month_sha = commit_before(root, (today or date.today()) - timedelta(days=MASK_GROWTH_DAYS))
    trees: list[_Tree] = []
    try:
        trees.append(_load_rev(root, base_rev))
        if month_sha:
            trees.append(_load_rev(root, month_sha))
        return history_violations(trees[0].repo, Repo.load(root), approved=approved, aa=aa,
                                  month_ago=trees[1].repo if len(trees) > 1 else None)
    finally:
        for t in trees:
            t.tmp.cleanup()


def lint_changed(root: str | Path, base_rev: str, phase: int = 0, today: date | None = None, *,
                 approved: Iterable[str] | None = None, aa: Mapping[tuple[str, str], AAResult] | None = None
                 ) -> tuple[LintReport, frozenset[str]]:
    """`sentinel lint --changed BASE` on a sentinel-config pull request (4.15, choice 1): (report, changed files).
    The history rules run against BASE and against the tree of 30 days ago and leave the skipped list."""
    root = Path(root)
    today = today or date.today()
    full = lint_repo(Repo.load(root), phase, today)
    changed = changed_files(root, base_rev)
    history = history_at(root, base_rev, approved=approved, aa=aa, today=today)
    kept = [v for v in full.violations if v.severity == "error" or v.file in changed]
    skipped = {k: v for k, v in full.skipped.items() if k not in HISTORY_RULES}
    return LintReport(sorted(set(kept + history)), skipped, full.expired), changed


def review_comment(report: LintReport) -> dict[str, Any]:
    """The review comment a `--changed` run posts for its warnings (4.15: warn is posted as a review comment)."""
    warns = report.warnings
    lines = [f"- `{v.rule}` {v.file}" + (f" ({v.path})" if v.path else "") + f": {v.message}" for v in warns]
    body = ("sentinel lint: no warnings" if not warns else
            f"sentinel lint: {len(warns)} warning{'s' if len(warns) != 1 else ''}\n\n" + "\n".join(lines))
    return {"body": body, "warnings": [v.as_dict() for v in warns]}


# -- --file --------------------------------------------------------------------------------------------------------

def lint_file(repo: Repo, path: str | Path, phase: int = 0, today: date | None = None) -> list[Violation]:
    """`sentinel lint --file <flow>` (4.15): every violation of one flow file linted in the context of the config
    checkout `repo`. The file replaces the flow of the same path or `flow_id`; a file from elsewhere is placed under
    `flows/quarantine/` when it is an Explorer candidate, else under `flows/tier-<tier>/`."""
    p = Path(path)
    rel = None
    try:
        rel = p.resolve().relative_to(repo.root.resolve()).as_posix()
    except ValueError:
        pass
    doc = _read(p, rel or p.name, "flow")
    data = _d(doc.data)
    r = Repo(repo.root, dict(repo.docs))
    same = next((k for k, d in r.docs.items() if d.kind == "flow" and _d(d.data).get("flow_id") == data.get("flow_id")
                 and data.get("flow_id") is not None), None)
    if rel is None:
        rel = same or (f"flows/quarantine/{p.name}" if data.get("class") == "explorer_candidate"
                       else f"flows/tier-{data.get('tier', 1)}/{p.name}")
    if same is not None and same != rel:
        r.docs.pop(same)
    r.docs[rel] = Doc(rel, "flow", doc.data, doc.error)
    return sorted(v for v in lint_repo(r, phase, today).violations if v.file == rel)


def explorer_lint_failed(finding: Finding, violations: Iterable[Violation]) -> Finding | None:
    """A failing Explorer candidate (4.15, 2.9): the `explorer_candidate` finding kept with `replayed: lint_failed` and
    the lint output in its evidence; None when the candidate has no error (it may be replayed)."""
    errors = [v for v in violations if v.severity == "error"]
    if not errors:
        return None
    if finding.kind != "explorer_candidate" or finding.explorer is None:
        raise ValueError("lint_failed applies to an explorer_candidate finding with its explorer block (4.13)")
    lint_lines = [v.line() for v in sorted(violations)]
    return dataclasses.replace(finding, explorer=dataclasses.replace(finding.explorer, replayed="lint_failed"),
                               evidence={**finding.evidence, "lint": lint_lines})


# -- --manifest ----------------------------------------------------------------------------------------------------

def lint_manifest_file(repo: Repo, path: str | Path, phase: int = 1, today: date | None = None, *,
                       base_registry_ids: Iterable[str] | None = None,
                       ticket_types: Mapping[str, str] | None = None) -> list[Violation]:
    """`sentinel lint --manifest .sentinel/manifest.yaml` (4.3, 4.15): the manifest linted against the config checkout
    as its run copy `manifests/<candidate>.yaml`, replacing any copy there; the candidate build's absence from builds/
    is not reported (choice 7). With the base build's registry ids, L-MAN-10 too."""
    p = Path(path)
    doc = _read(p, f"manifests/{p.name}", "manifest")
    data = _d(doc.data)
    cand = data.get("candidate")
    rel = f"manifests/{cand}.yaml" if isinstance(cand, str) and cand and "/" not in cand else doc.rel
    r = Repo(repo.root, {k: d for k, d in repo.docs.items() if not (d.kind == "manifest" and k == rel)})
    r.docs[rel] = Doc(rel, "manifest", doc.data, doc.error)
    found = [v for v in lint_repo(r, phase, today).violations
             if v.file == rel and not (v.rule == "L-MAN-00" and v.path == "candidate")]
    if base_registry_ids is not None and not doc.error:
        found += lint_manifest_against_base(data, base_registry_ids, ticket_types, rel)
    return sorted(set(found))


def manifest_status(violations: Iterable[Violation], phase: int) -> tuple[dict[str, Any], int]:
    """The `sentinel/manifest` commit status (4.3, choice 7) and the exit code: 1 on an error from Phase 2, else 0."""
    errors = [v for v in violations if v.severity == "error"]
    blocking = phase >= MANIFEST_BLOCKING_PHASE
    state = "failure" if errors else "success"
    rules = sorted({v.rule for v in errors})
    desc = ("manifest lints clean" if not errors else
            f"{len(errors)} error{'s' if len(errors) != 1 else ''}: {', '.join(rules)}" + ("" if blocking else "; informational"))
    status = {"context": MANIFEST_CONTEXT, "state": state, "description": desc, "blocking": blocking,
              "violations": [v.as_dict() for v in sorted(violations)]}
    return status, (1 if errors and blocking else 0)


def lint_manifest_against_base(manifest: Mapping[str, Any], base_registry_ids: Iterable[str],
                               ticket_types: Mapping[str, str] | None = None, file: str = "manifest") -> list[Violation]:
    """L-MAN-10 (4.3, choice 8): `new_surface: true` needs a declared surface absent from the base build's registry or
    a feature ticket; `new_surface: false` (the default) on a declared surface the base registry lacks is an error.
    `base_registry_ids` are the surface ids of the config commit pinned for the base build (8.5)."""
    base = set(base_registry_ids)
    types = {str(k): str(v).casefold() for k, v in (ticket_types or {}).items()}
    out = []
    for i, e in enumerate(map(_d, _l(_d(manifest).get("entries")))):
        absent = [s for s in _l(e.get("declared_surfaces")) if s not in base]
        feature = any(types.get(str(t)) in FEATURE_TICKET_TYPES for t in _l(e.get("tickets")))
        at = f"entries[{i}].new_surface"
        if e.get("new_surface") is True and not absent and not feature:
            out.append(_v("L-MAN-10", file, "new_surface: true, but every declared surface exists on the base build's "
                                            "registry and no ticket is a feature ticket", at))
        if e.get("new_surface", False) is not True and absent:
            out.append(_v("L-MAN-10", file, f"{', '.join(map(str, absent))} absent from the base build's registry; the "
                                            "entry carries new_surface: true", at))
    return out


def registry_ids_at(root: str | Path, rev: str) -> frozenset[str]:
    """The surface ids of the config tree at `rev` (the base build's registry commit, 8.5)."""
    tree = _load_rev(Path(root), rev)
    try:
        return frozenset(str(_d(d.data).get("id")) for d in tree.repo.of("surface") if _d(d.data).get("id"))
    finally:
        tree.tmp.cleanup()


# -- run start: --config-commit, the store and the run context ----------------------------------------------------

class DecisionSource(Protocol):
    """What L-MSK-01 reads from the Central Store (sentinel.store.Store has it)."""

    def get_decision(self, decision_id: str) -> Any: ...


def store_rules(repo: Repo, store: DecisionSource | None) -> list[Violation]:
    """L-MSK-01 against the Central Store (4.8, choice 5): each mask's `decision_ref` is a `request_mask` decision
    made by a human in the triage roster of ops/oncall.yaml. Nothing to check without a store (CI checks
    created_by only)."""
    if store is None:
        return []
    from ..records import NON_HUMAN
    from ..store.store import Roster      # lazy: the store package is large

    oncall = repo.one("oncall")
    roster = Roster.from_oncall(_d(oncall.data)) if oncall else None
    out = []
    for doc in repo.of("mask"):
        m = _d(doc.data)
        ref = m.get("decision_ref")
        try:
            dec = store.get_decision(str(ref)) if ref else None
        except (ValueError, KeyError, TypeError):
            dec = None
        if dec is None:
            out.append(_v("L-MSK-01", doc.rel, f"decision_ref {ref} is not a decision of the Central Store", "decision_ref"))
            continue
        if dec.action != "request_mask":
            out.append(_v("L-MSK-01", doc.rel, f"decision {ref} is {dec.action}, not request_mask", "decision_ref"))
        if dec.decided_by in NON_HUMAN:
            out.append(_v("L-MSK-01", doc.rel, f"decision {ref} was made by {dec.decided_by!r}, not a human", "decision_ref"))
        elif roster is None:
            out.append(_v("L-MSK-01", doc.rel, "ops/oncall.yaml is missing: no triage roster to check the decider "
                                               "against", "decision_ref"))
        elif dec.decided_by not in roster.triage:
            out.append(_v("L-MSK-01", doc.rel, f"decision {ref} was made by {dec.decided_by}, who is not in the triage "
                                               "roster of ops/oncall.yaml", "decision_ref"))
    return out


def run_context_rules(repo: Repo, context: Mapping[str, Any]) -> list[Violation]:
    """L-XREF-16 (choice 6) and L-MAN-09 against the run context: `authorization_ref` and `authorization_sha256` of the
    run manifest, and `candidate` with `manifest` (the run's manifest copy, repository relative) when given."""
    out = []
    ref, sha = str(context.get("authorization_ref") or ""), context.get("authorization_sha256")
    version = Path(ref).name.removesuffix(".md").removesuffix(".yaml") if ref else ""
    md, rec = f"authorization/{version}.md", f"authorization/{version}.yaml"
    if not version or not sha:
        out.append(_v("L-XREF-16", "run", "the run manifest carries no authorization reference and sha256 (6.12); "
                                         "preflight refuses the run (11.7)", "authorization_ref"))
    elif not (repo.exists(md) and repo.exists(rec)):
        out.append(_v("L-XREF-16", "run", f"authorization {version} has no {md} and {rec} in the config commit",
                      "authorization_ref"))
    elif file_sha256(repo.path(md)) != sha:
        out.append(_v("L-XREF-16", "run", f"authorization_sha256 is not the sha256 of {md}", "authorization_sha256"))
    manifest, cand = context.get("manifest"), context.get("candidate")
    if isinstance(manifest, str) and isinstance(cand, str):
        from .runstart import lint_manifest
        out += [v for v in lint_manifest(repo, manifest, candidate=cand) if v.rule == "L-MAN-09"]
    return out


def lint_config_commit(root: str | Path, sha: str, phase: int = 1, today: date | None = None, *,
                       context: Mapping[str, Any] | None = None, store: DecisionSource | None = None) -> LintReport:
    """`sentinel lint --all --config-commit <sha>` (4.15, 12.7 G1): the run-start lint of the config tree at `sha`
    (sentinel.lint.runstart: expired records in `LintReport.expired`, never violations; manifests left to
    lint_manifest), plus the run-context rules and L-MSK-01 against the store when given. Any error aborts the run
    `aborted_harness (reason: config_invalid)`."""
    tree = _load_rev(Path(root), sha)
    try:
        report, expired = lint_at_run_start(tree.repo, phase, today)
        extra = store_rules(tree.repo, store) + (run_context_rules(tree.repo, context) if context is not None else [])
    finally:
        tree.tmp.cleanup()
    skipped = dict(report.skipped)
    if context is not None:
        skipped.pop("L-XREF-16", None)
        skipped.pop("L-MAN-09", None)
    return LintReport(sorted(set(report.violations + extra)), skipped, expired)


def load_aa_results(path: str | Path) -> dict[tuple[str, str], AAResult]:
    """A/A results from JSON: a list of {checkpoint_id, region_id, run_id, changed_px} (the CLI's --aa-results)."""
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LintModeError(f"cannot read A/A results {path}: {exc}") from exc
    return {(str(r["checkpoint_id"]), str(r["region_id"])): AAResult(str(r["run_id"]), int(r["changed_px"]))
            for r in rows}


def _iter_lines(path: str | Path) -> Iterator[str]:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            yield line


def load_approved(path: str | Path) -> frozenset[str]:
    """Approved checkpoint ids, one per line (or a JSON list), for L-XREF-10 (the CLI's --approved)."""
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("["):
        return frozenset(map(str, json.loads(text)))
    return frozenset(_iter_lines(path))


__all__ = [
    "AAResult", "DecisionSource", "HISTORY_RULES", "LintModeError", "changed_files", "commit_before",
    "explorer_lint_failed", "follow_renames", "history_at", "history_violations", "lint_changed", "lint_config_commit",
    "lint_file",
    "lint_manifest_against_base", "lint_manifest_file", "load_aa_results", "load_approved", "manifest_status",
    "registry_ids_at", "renames_of", "review_comment", "run_context_rules", "store_rules", "tree_at",
]
