"""Retention of the Central Store's artifacts and step events (8.6 Retention and backup, 8.9, SD-82, SD-83).

The nightly job runs after the backup (`store.backup.run_backup`) on the control host:

- `refresh_pins(store)` sets `artifacts.pinned` (8.3: "referenced by a baseline or an unclosed finding"): every
  hash a baseline row names (image or sidecar, any state) and every hash a finding that pins its evidence
  references (SD-83: `open`, `confirmed_bug`, `fixed_pending_verify`, or `intended` with no approved baseline yet).
  The backup mirrors and the weekly audit re-hashes exactly these objects.
- `select_candidates(store, now, pr_closed_at=)` applies the class rows of the 8.6 table, per set (SD-82): one
  `Candidate` per live `artifact_sets` copy whose class has expired. It does not look at pins or references;
  that is the sweep's safety rule, so a pinned object the class would remove is counted (8.9).
- `sweep(store, artifact_root, candidates, backup_ok=, now=)` removes what the safety rules allow: it sets
  `artifact_sets.deleted_at` before unlinking that set's copy, `artifacts.deleted_at` once no live copy of the hash
  remains, and keeps (and counts) a copy that is quarantined, pinned, named by a baseline row, referenced by a
  finding that pins its evidence or released by its finding less than 180 days ago. Nothing runs on a night whose
  backup failed (`RetentionResult.health_rows`: "retention skipped: backup failed").
- `prune_step_events(store, now)` deletes the step events of runs older than 90 days, except runs with a finding
  that pins its evidence, a `blocked_by_gateway` step, an incident or an `aborted_safety` outcome.
- `rig_releasable(store, now, last_revert_at=, released=)` is the rig-side rule: a shard directory may be removed
  from its rig once imported plus 7 days have passed and the rig has reverted to its snapshot since the import; an
  `import_failed`, `lost` or `superseded` shard only once a human released it.
- `ensure_set_rows(store, artifact_root=)` records the first set's copy of every indexed object that has no
  `artifact_sets` row for it yet and, with the artifact root, every unrecorded copy of an indexed object on disk
  under a finished run's set, so every copy is handled per set (8.4, SD-82); the sweep runs it first.

Choices where the spec is ambiguous or silent (reported to the coordinator):
- A finding references a hash when the hash appears in its `evidence_json` (the 4.13 `artifacts` list and any
  other evidence key) or its `explorer_json` (the 4.13 explorer block: `signature.crop_sha256`, `trace_ref`), or
  is a capture or heatmap of the outcome of the finding's run and checkpoint.
- The 180 days run from `closed_at` when set; otherwise from the time the finding left the pinning states as the
  store records it: the approval of a baseline of its checkpoint at or after the finding (`intended`), or its
  decision's `decided_at`, never earlier than the finding's `created_at` (closed, noise, agent_mistake,
  expected_behavior). A triage state outside the vocabulary of SD-83 pins.
- "An open finding" (step_events row of the table) is a finding that pins its evidence (SD-83), the same rule as
  for artifacts; a run's age is its `started_at`.
- Class clocks start at the run's end (`finished_at`, else `started_at`); a run that is not finished, a set that
  is not `runs/<run_id>` (baselines, eval sets: never deleted; eval items live as long as the eval set) and a run
  the store does not know are never candidates. Kind rows override the run-type row where the table names the
  kind: crash and log slices 180 days, reviewer requests 30 days; agent frames and video at most 14 days (the
  Nightly row's 7 days stays the shorter); ledgers, journals and manifests live as long as the rest of their set.
- Every merge run is "on main" (merge runs are post-merge runs of main, 11.2). An RC run's release is its build
  pair's base build, the released build every RC run of that release is compared against (9.8 lineage); RC runs
  outside the last two of their release are superseded and their class has expired.
- The next calibration is the next Calibration run (by `started_at`) whose outcome is `complete`; without one a
  Calibration run's captures are kept.
- PR close times are not stored (8.3 has no column): the caller passes `pr_closed_at` (pr_ref -> merge or close
  time) from the PR host; without it the 14 days apply.
- A run set whose copies are all deleted loses its remaining files too (manifest, `shards/` ledgers and journals:
  "life of the run's artifact set"), unless it carries the quarantine marker.
- Deleting step events goes through a writer batch (`Store.batch`, `WriteBatch.delete`), journaled to
  results.jsonl like every write.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

from .artifacts import QUARANTINE_MARKER, SHA256_RE
from .store import RELEASING_TRIAGE, Store, utc

PIN_STATES = ("open", "confirmed_bug", "fixed_pending_verify")          # SD-83; intended pins until its baseline
RELEASE_STATES = RELEASING_TRIAGE
RELEASE_DAYS = 180
STEP_EVENT_DAYS = 90
RIG_KEEP_DAYS = 7
RUN_DAYS = {"pr": 14, "merge": 14, "nightly": 7}
PR_CLOSED_DAYS = 7
CALIBRATION_GRACE_DAYS = 30
KIND_DAYS = {"crash": 180, "log_slice": 180, "request": 30}           # kind rows that replace the run-type row
FRAME_KINDS = ("agent_frame", "video")
FRAME_DAYS = 14
SET_LIFE_KINDS = ("ledger", "journal", "manifest")                    # "life of the run's artifact set"
KEEP_MERGE_RUNS = 3
KEEP_RC_RUNS = 2
HUMAN_RELEASE_STATES = ("import_failed", "lost", "superseded")
OUTCOME_HASH_COLUMNS = ("cand_capture_sha256", "base_capture_sha256", "heatmap_sha256")
_HASH = re.compile(r"[0-9a-f]{64}")
_RUN_SET = re.compile(r"^runs/([A-Za-z0-9._-]+)$")


def ts(value: str) -> datetime:
    """A stored timestamp as an aware datetime (the store writes UTC ISO 8601, 8.3)."""
    return datetime.fromisoformat(utc(value))


def object_file(set_root: Path, sha256: str) -> Path | None:
    """The object file of `sha256` under a set directory (`objects/<aa>/<sha256>.<ext>`, 8.4), or None."""
    if not SHA256_RE.match(sha256):
        raise ValueError(f"not a sha256: {sha256!r}")
    d = set_root / "objects" / sha256[:2]
    if not d.is_dir():
        return None
    for p in sorted(d.iterdir()):
        if p.is_file() and (p.name == sha256 or p.name.startswith(sha256 + ".")):
            return p
    return None


def run_of_set(set_path: str) -> str | None:
    """The run id of a `runs/<run_id>` set, else None (8.4)."""
    m = _RUN_SET.match(set_path)
    return m.group(1) if m else None


@dataclass(frozen=True)
class LiveCopy:
    """One `artifact_sets` copy not yet deleted, with its object's index row (SD-82)."""
    sha256: str
    set_path: str
    kind: str
    pinned: bool
    audit_state: str
    quarantined: bool


def live_copies(store: Store) -> list[LiveCopy]:
    """Every copy whose `deleted_at` is NULL, in (set, hash) order."""
    rows = store.query("SELECT s.sha256, s.set_path, s.quarantined_at, a.kind, a.pinned, a.audit_state "
                       "FROM artifact_sets s JOIN artifacts a ON a.sha256 = s.sha256 WHERE s.deleted_at IS NULL "
                       "ORDER BY s.set_path, s.sha256")
    return [LiveCopy(r["sha256"], r["set_path"], r["kind"], bool(r["pinned"]), r["audit_state"],
                     r["quarantined_at"] is not None) for r in rows]


def _unrecorded_on_disk(store: Store, artifact_root: Path) -> list[dict]:
    """The `artifact_sets` rows missing for object files under finished runs' sets (`runs/<run_id>/objects/`, 8.4)
    whose hash is indexed but has no row for that set; a quarantined set is left alone (6.9)."""
    finished = {r[0] for r in store.query("SELECT run_id FROM runs WHERE lifecycle = 'finished'")}
    base = artifact_root / "runs"
    out: list[dict] = []
    for d in sorted(base.iterdir()) if base.is_dir() else ():
        if d.name not in finished or not (d / "objects").is_dir() or (d / QUARANTINE_MARKER).exists():
            continue
        on_disk = {}
        for p in sorted((d / "objects").glob("*/*")):
            sha = p.name.split(".", 1)[0]
            if SHA256_RE.match(sha) and p.parent.name == sha[:2] and p.is_file():
                on_disk.setdefault(sha, p)
        set_path = f"runs/{d.name}"
        known = {r[0] for r in store.query("SELECT sha256 FROM artifact_sets WHERE set_path = ?", (set_path,))}
        for sha in sorted(set(on_disk) - known):
            if store.query("SELECT 1 FROM artifacts WHERE sha256 = ?", (sha,)):
                made = datetime.fromtimestamp(on_disk[sha].stat().st_mtime, timezone.utc).replace(microsecond=0)
                out.append({"sha256": sha, "set_path": set_path, "created_at": made.isoformat(), "deleted_at": None})
    return out


def ensure_set_rows(store: Store, artifact_root: str | Path | None = None) -> int:
    """Record the copies the store has no `artifact_sets` row for (8.4: the import writes the row of every copy;
    SD-82), returning how many rows were written: the copy in its first set (`artifacts.set_path`) of every object,
    recorded deleted when the object is; with `artifact_root`, also every object file of an indexed hash under a
    finished run's set that has no row for that set (a store imported before every copy had its row), recorded
    live. A hash with a live copy then loses its `deleted_at` (8.7: "retention removed it" only while every copy
    is deleted)."""
    rows = [dict(r) for r in store.query(
        "SELECT a.sha256, a.set_path, a.created_at, a.deleted_at FROM artifacts a WHERE NOT EXISTS "
        "(SELECT 1 FROM artifact_sets s WHERE s.sha256 = a.sha256 AND s.set_path = a.set_path)")]
    if artifact_root is not None:
        rows += _unrecorded_on_disk(store, Path(artifact_root))
    if rows:
        with store.batch("artifact_set_backfill") as b:
            for r in rows:
                b.insert("artifact_sets", r)
    live = [r[0] for r in store.query("SELECT a.sha256 FROM artifacts a WHERE a.deleted_at IS NOT NULL AND EXISTS "
                                      "(SELECT 1 FROM artifact_sets s WHERE s.sha256 = a.sha256 AND s.deleted_at "
                                      "IS NULL)")]
    if live:
        with store.batch("artifact_live_again") as b:
            for sha in live:
                b.update("artifacts", {"sha256": sha}, {"deleted_at": None})
    return len(rows)


# -- what pins evidence (8.3, 8.6, SD-83) --------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    """The references that protect objects: baseline hashes, hashes a pinning finding references, the latest
    release time of every hash only released findings reference, and the runs with a pinning finding."""
    baseline: frozenset[str]
    pinned: frozenset[str]
    released: Mapping[str, datetime]
    pinning_runs: frozenset[str]

    def protects(self, sha256: str, now: datetime) -> str | None:
        """Why the copy must stay (8.6 safety rule), or None."""
        if sha256 in self.baseline:
            return "named by a baseline row"
        if sha256 in self.pinned:
            return "referenced by a finding that pins its evidence"
        rel = self.released.get(sha256)
        if rel is not None and now < rel + timedelta(days=RELEASE_DAYS):
            return f"released by its finding at {rel.isoformat()}, kept {RELEASE_DAYS} days"
        return None


def _approvals(store: Store) -> dict[str, list[datetime]]:
    out: dict[str, list[datetime]] = {}
    for r in store.query("SELECT checkpoint_id, approved_at FROM baselines WHERE approved_at IS NOT NULL"):
        out.setdefault(r["checkpoint_id"], []).append(ts(r["approved_at"]))
    return {k: sorted(v) for k, v in out.items()}


def _finding_hashes(r) -> set[str]:
    found = set(_HASH.findall(r["evidence_json"] or ""))
    found.update(_HASH.findall(r["explorer_json"] or ""))       # 4.13: signature.crop_sha256, trace_ref
    found.update(r[c] for c in OUTCOME_HASH_COLUMNS if r[c])
    return found


def _release_time(r, approvals: Mapping[str, list[datetime]]) -> datetime | None:
    """None while the finding pins its evidence (SD-83), else the time it left the pinning states."""
    state, created = r["triage_state"], ts(r["created_at"])
    if state == "intended":
        approved = next((a for a in approvals.get(r["checkpoint_id"], ()) if a >= created), None)
        if approved is None:
            return None
        return ts(r["closed_at"]) if r["closed_at"] else approved
    if state not in RELEASE_STATES:
        return None
    if r["closed_at"]:
        return ts(r["closed_at"])
    return max(created, ts(r["decided_at"])) if r["decided_at"] else created


def evidence(store: Store) -> Evidence:
    """Read every baseline and finding once and say which hashes they protect (8.6, SD-83)."""
    baseline = {h for r in store.query("SELECT image_sha256, sidecar_sha256 FROM baselines")
                for h in (r["image_sha256"], r["sidecar_sha256"]) if h}
    approvals = _approvals(store)
    pinned: set[str] = set()
    released: dict[str, datetime] = {}
    runs: set[str] = set()
    sql = ("SELECT f.run_id, f.checkpoint_id, f.triage_state, f.created_at, f.closed_at, f.evidence_json, "
           "f.explorer_json, d.decided_at, " + ", ".join(f"o.{c}" for c in OUTCOME_HASH_COLUMNS) + " FROM findings f "
           "LEFT JOIN decisions d ON d.decision_id = f.decision_id "
           "LEFT JOIN outcomes o ON o.run_id = f.run_id AND o.checkpoint_id = f.checkpoint_id")
    for r in store.query(sql):
        hashes = _finding_hashes(r)
        rel = _release_time(r, approvals)
        if rel is None:
            pinned |= hashes
            runs.add(r["run_id"])
            continue
        for h in hashes:
            if h not in released or released[h] < rel:
                released[h] = rel
    return Evidence(frozenset(baseline), frozenset(pinned), {h: t for h, t in released.items() if h not in pinned},
                    frozenset(runs))


@dataclass(frozen=True)
class PinResult:
    """What `refresh_pins` found and changed."""
    pinned: int
    changed: int


def refresh_pins(store: Store) -> PinResult:
    """Set `artifacts.pinned` from the baselines and the findings that pin their evidence (8.3, SD-83); only rows
    whose flag changes are written, in one batch."""
    ev = evidence(store)
    want = ev.baseline | ev.pinned
    changes = [(r["sha256"], int(r["sha256"] in want)) for r in store.query("SELECT sha256, pinned FROM artifacts")
               if bool(r["pinned"]) != (r["sha256"] in want)]
    if changes:
        with store.batch("pins") as b:
            for sha, flag in changes:
                b.update("artifacts", {"sha256": sha}, {"pinned": flag})
    pinned = store.query("SELECT count(*) FROM artifacts WHERE pinned = 1")[0][0]
    return PinResult(pinned, len(changes))


# -- the class rows of the 8.6 table -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """A set copy whose class has expired (8.6), before the sweep's safety rule."""
    sha256: str
    set_path: str
    kind: str
    rule: str
    expired_at: str


def protected_runs(store: Store) -> frozenset[str]:
    """The full checkpoint sets 8.6 keeps until superseded: the last 3 `complete` merge runs and the last 2 RC
    runs of each release (the RC build pair's base build)."""
    keep = {r[0] for r in store.query(
        "SELECT run_id FROM runs WHERE run_type = 'merge' AND outcome = 'complete' "
        "ORDER BY started_at DESC, run_id DESC LIMIT ?", (KEEP_MERGE_RUNS,))}
    seen: dict[str | None, int] = {}
    for r in store.query("SELECT r.run_id, p.base_build_id FROM runs r LEFT JOIN build_pairs p ON p.pair_id = "
                         "r.pair_id WHERE r.run_type = 'rc' ORDER BY r.started_at DESC, r.run_id DESC"):
        n = seen.get(r["base_build_id"], 0)
        if n < KEEP_RC_RUNS:
            keep.add(r["run_id"])
        seen[r["base_build_id"]] = n + 1
    return frozenset(keep)


def _next_calibration(store: Store) -> dict[str, datetime]:
    """Calibration run -> when the next complete Calibration run finished."""
    runs = store.query("SELECT run_id, started_at, outcome, finished_at FROM runs WHERE run_type = 'calibration' "
                       "ORDER BY started_at, run_id")
    out: dict[str, datetime] = {}
    for i, r in enumerate(runs):
        nxt = next((n for n in runs[i + 1:] if n["outcome"] == "complete" and n["finished_at"]), None)
        if nxt is not None:
            out[r["run_id"]] = ts(nxt["finished_at"])
    return out


def _class_expiry(run, kind: str, *, next_cal: Mapping[str, datetime],
                  pr_closed: Mapping[str, str]) -> tuple[datetime | None, str]:
    """When the class of a copy of `kind` in `run`'s set expires (None: kept), and the row that says so."""
    end = ts(run["finished_at"] or run["started_at"])
    if kind in KIND_DAYS:
        return end + timedelta(days=KIND_DAYS[kind]), f"{kind}: {KIND_DAYS[kind]} days"
    rt = run["run_type"]
    if rt in RUN_DAYS:
        expiry, rule = end + timedelta(days=RUN_DAYS[rt]), f"{rt} run: {RUN_DAYS[rt]} days"
        closed = pr_closed.get(run["pr_ref"]) if rt == "pr" and run["pr_ref"] else None
        if closed is not None and ts(closed) + timedelta(days=PR_CLOSED_DAYS) < expiry:
            expiry, rule = ts(closed) + timedelta(days=PR_CLOSED_DAYS), f"pr run: {PR_CLOSED_DAYS} days after close"
    elif rt == "calibration":
        nxt = next_cal.get(run["run_id"])
        expiry = None if nxt is None else nxt + timedelta(days=CALIBRATION_GRACE_DAYS)
        rule = f"calibration run: next calibration plus {CALIBRATION_GRACE_DAYS} days"
    else:                                         # rc: outside the last two of its release, superseded
        expiry, rule = end, "rc run: superseded"
    if kind in FRAME_KINDS and (expiry is None or expiry > end + timedelta(days=FRAME_DAYS)):
        expiry, rule = end + timedelta(days=FRAME_DAYS), f"{kind}: {FRAME_DAYS} days"
    return expiry, rule


def select_candidates(store: Store, now: str, *, pr_closed_at: Mapping[str, str] | None = None
                      ) -> tuple[Candidate, ...]:
    """The live, unquarantined copies in run sets whose 8.6 class has expired at `now`, in (set, hash) order.
    Pins and references are not consulted here: the sweep keeps and counts those (8.9)."""
    at = ts(now)
    runs = {r["run_id"]: r for r in store.query(
        "SELECT run_id, run_type, lifecycle, started_at, finished_at, pr_ref FROM runs")}
    keep = protected_runs(store)
    next_cal = _next_calibration(store)
    by_set: dict[str, list[LiveCopy]] = {}
    for c in live_copies(store):
        by_set.setdefault(c.set_path, []).append(c)
    out: list[Candidate] = []
    for set_path, copies in sorted(by_set.items()):
        run = runs.get(run_of_set(set_path) or "")
        if run is None or run["lifecycle"] != "finished" or run["run_id"] in keep:
            continue
        chosen: list[Candidate] = []
        whole = True
        for c in copies:
            if c.quarantined or c.kind in SET_LIFE_KINDS:
                whole = whole and c.kind in SET_LIFE_KINDS
                continue
            expiry, rule = _class_expiry(run, c.kind, next_cal=next_cal, pr_closed=pr_closed_at or {})
            if expiry is not None and expiry <= at:
                chosen.append(Candidate(c.sha256, set_path, c.kind, rule, expiry.isoformat()))
            else:
                whole = False
        if whole:                                 # the rest of the set goes, so its ledgers go with it
            last = max((ch.expired_at for ch in chosen), default=utc(run["finished_at"] or run["started_at"]))
            chosen += [Candidate(c.sha256, set_path, c.kind, "ledger: life of the run's artifact set", last)
                       for c in copies if c.kind in SET_LIFE_KINDS and not c.quarantined]
        out += chosen
    return tuple(out)


# -- the sweep (8.6, 8.9) ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Kept:
    """A candidate the safety rule kept, and why (8.9: "object kept; count reported")."""
    candidate: Candidate
    reason: str


@dataclass(frozen=True)
class RetentionResult:
    """What one sweep did."""
    deleted: tuple[Candidate, ...] = ()
    kept: tuple[Kept, ...] = ()
    skipped: bool = False

    def health_rows(self) -> list[tuple[str, str]]:
        """The Run health row (8.6, 8.9)."""
        if self.skipped:
            return [("Retention", "retention skipped: backup failed")]
        return [("Retention", f"{len(self.deleted)} deleted; {len(self.kept)} kept: pinned or referenced")]


def _live(store: Store, sha256: str, set_path: str | None = None) -> list:
    if set_path is None:
        return store.query("SELECT set_path, quarantined_at FROM artifact_sets WHERE sha256 = ? AND deleted_at "
                           "IS NULL", (sha256,))
    return store.query("SELECT s.quarantined_at, a.pinned FROM artifact_sets s JOIN artifacts a ON a.sha256 = "
                       "s.sha256 WHERE s.sha256 = ? AND s.set_path = ? AND s.deleted_at IS NULL", (sha256, set_path))


def _set_lives(store: Store, set_path: str) -> bool:
    """True while the set holds a live copy that is not a ledger, journal or manifest."""
    return bool(store.query("SELECT 1 FROM artifact_sets s JOIN artifacts a ON a.sha256 = s.sha256 WHERE "
                            "s.set_path = ? AND s.deleted_at IS NULL AND a.kind NOT IN (?, ?, ?) LIMIT 1",
                            (set_path, *SET_LIFE_KINDS)))


def _retire_set(store: Store, artifact_root: Path, set_path: str) -> None:
    """Remove what is left of a run set (manifest, `shards/` ledgers and journals) once no copy of it lives."""
    root = artifact_root / set_path
    if run_of_set(set_path) is None or not root.is_dir() or (root / QUARANTINE_MARKER).exists():
        return
    if store.query("SELECT 1 FROM artifact_sets WHERE set_path = ? AND deleted_at IS NULL LIMIT 1", (set_path,)):
        return
    shutil.rmtree(root)


def sweep(store: Store, artifact_root: str | Path, candidates: Iterable[Candidate], *, backup_ok: bool,
          now: str) -> RetentionResult:
    """Remove the candidates the 8.6 safety rule allows; keep and count the rest. Nothing runs when tonight's
    backup failed (`store.backup.backup_ok`). Every copy of an indexed object on disk under a finished run's set is
    recorded first (`ensure_set_rows`), so a hash is marked deleted only when no copy of it is left and a set is
    retired only when none of its copies lives. Ledger, journal and manifest copies go only after every other copy
    of their set; one whose set still lives waits for a later night (neither deleted nor counted)."""
    if not backup_ok:
        return RetentionResult(skipped=True)
    at, root = ts(now), Path(artifact_root)
    stamp = at.isoformat()
    ensure_set_rows(store, root)                  # every copy on disk is handled per set before anything goes
    ev = evidence(store)
    deleted: list[Candidate] = []
    kept: list[Kept] = []
    touched: set[str] = set()
    for c in sorted(candidates, key=lambda c: (c.kind in SET_LIFE_KINDS, c.set_path, c.sha256)):
        row = _live(store, c.sha256, c.set_path)
        if not row:
            continue
        reason = ("quarantined: until security clears it" if row[0]["quarantined_at"] is not None
                  else "pinned" if row[0]["pinned"] else ev.protects(c.sha256, at))
        if reason is not None:
            kept.append(Kept(c, reason))
            continue
        if c.kind in SET_LIFE_KINDS and _set_lives(store, c.set_path):
            continue
        others = [r for r in _live(store, c.sha256) if r["set_path"] != c.set_path]
        with store.batch("retention") as b:
            b.update("artifact_sets", {"sha256": c.sha256, "set_path": c.set_path}, {"deleted_at": stamp})
            if not others:
                b.update("artifacts", {"sha256": c.sha256}, {"deleted_at": stamp})
        path = object_file(root / c.set_path, c.sha256)
        if path is not None:
            path.unlink()
        deleted.append(c)
        touched.add(c.set_path)
    for set_path in sorted(touched):
        _retire_set(store, root, set_path)
    return RetentionResult(tuple(deleted), tuple(kept))


# -- step events (8.6) ---------------------------------------------------------------------------------------------

def _exempt_runs(store: Store) -> set[str]:
    """Runs whose step events are kept indefinitely (8.6)."""
    runs = set(evidence(store).pinning_runs)
    for sql in ("SELECT DISTINCT run_id FROM step_events WHERE step_outcome = 'blocked_by_gateway'",
                "SELECT DISTINCT run_id FROM incidents WHERE run_id IS NOT NULL",
                "SELECT run_id FROM runs WHERE outcome = 'aborted_safety'",
                "SELECT DISTINCT run_id FROM shards WHERE shard_outcome = 'aborted_safety'"):
        runs.update(r[0] for r in store.query(sql))
    return runs


def prune_step_events(store: Store, now: str) -> int:
    """Delete the step events of runs started more than 90 days before `now`, except the exempt runs; returns how
    many rows went (8.6)."""
    cutoff = (ts(now) - timedelta(days=STEP_EVENT_DAYS)).isoformat()
    exempt = _exempt_runs(store)
    old = [r[0] for r in store.query("SELECT r.run_id FROM runs r WHERE r.started_at < ? AND EXISTS (SELECT 1 FROM "
                                     "step_events e WHERE e.run_id = r.run_id) ORDER BY r.run_id", (cutoff,))
           if r[0] not in exempt]
    if not old:
        return 0
    with store.batch("retention_step_events") as b:
        return sum(b.delete("step_events", {"run_id": rid}) for rid in old)


# -- the rig side (8.6) --------------------------------------------------------------------------------------------

def rig_releasable(store: Store, now: str, *, last_revert_at: Mapping[str, str],
                   released: Iterable[str] = ()) -> tuple[str, ...]:
    """The shards whose Run Store, ledgers and objects may leave their rig (8.6): imported at least 7 days ago with
    a snapshot revert of the rig (`last_revert_at`: rig id -> time) after the import; an `import_failed`, `lost` or
    `superseded` shard only when a human released it (`released`)."""
    at, human = ts(now), set(released)
    out = []
    for r in store.query("SELECT shard_id, rig_id, state, imported_at FROM shards ORDER BY shard_id"):
        if r["state"] == "imported":
            imported, revert = ts(r["imported_at"]), last_revert_at.get(r["rig_id"])
            if at >= imported + timedelta(days=RIG_KEEP_DAYS) and revert is not None and ts(revert) > imported:
                out.append(r["shard_id"])
        elif r["state"] in HUMAN_RELEASE_STATES and r["shard_id"] in human:
            out.append(r["shard_id"])
    return tuple(out)

