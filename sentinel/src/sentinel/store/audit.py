"""The weekly artifact audit of the control host (8.6 Artifact audit, 8.7, 8.9, SD-82).

`run_audit(store, roots, backup_root, rng, now)` re-hashes every pinned object and a seeded 5 percent sample of the
rest, set by set: each live, unquarantined `artifact_sets` copy is checked on its own under its set directory
(`<root>/<set_path>/objects/<aa>/<sha256>.<ext>`, 8.4). For every copy that is missing or no longer hashes to its
name it opens an `artifact_missing` finding with the tier of the checkpoint whose outcome names the object (8.9);
a pinned copy that fails is restored from the backup mirror (`<backup_root>/<set_path>/...`, the layout
`store.backup` writes) when the mirror's copy verifies, and the restore is logged. Every checked hash gets
`audit_state` and `last_audit_at`; `system_state.last_artifact_audit_at` records the audit. While the Reporter
service runs it is the Central Store's only writer (8.2) and runs the audit with it (`run_audit(writer, ...)`),
held only to read the copies and to record the result. `main(argv)` is
`sentinel audit --db PATH --artifact-root DIR --backup-root DIR [--seed N] [--now TS]` when no service holds the
writer; with the writer held it writes nothing and exits 4.

Choices where the spec is ambiguous (reported to the coordinator):
- The sample is `ceil(5 percent)` of the copies that are not pinned, drawn with `rng.sample` from the copies in
  (set, hash) order, so a seed gives the same sample; `main` seeds with the ISO year and week of the audit, so a
  re-run in the same week checks the same copies and the next week other ones. Pins are refreshed first
  (`retention.refresh_pins`) and first-set copies recorded (`retention.ensure_set_rows`).
- `artifacts.audit_state` is per hash: the worst state among the copies checked this time (`missing` before
  `corrupt`); a copy restored from a verified mirror copy counts as `ok`, since its evidence is back (8.7 serves
  only `ok` hashes as evidence). The finding still records what was found, the restore and its source.
- The finding is the outcome's: the run and checkpoint of an outcome naming the hash as a capture or heatmap (in the
  set's run for a run set; the lowest tier first), else the checkpoint of a baseline row naming it (its run is the
  set's run or the baseline's source run), else the set's run or the hash's first run with no checkpoint and no
  tier. With no known run nothing can be written (findings need a run) and the failure is listed `unrecorded`.
- One finding per copy, keyed `fnd-<run>-artifact-missing-<set_path>-<sha256>`: a copy that fails again in a later
  audit keeps its finding, and every failure leaves it open and saying what this audit found (8.6, 8.9: triage
  learns of a failure from the finding). A finding triage moved out of `open` (closed once the evidence was back,
  for instance) is reopened (`triage_state` open, `closed_at` cleared), and one whose description or evidence no
  longer matches (restored before, not now; missing before, corrupt now) is rewritten; a failure that persists
  unchanged leaves its open finding as it is. Findings land on finished runs, so they are written through the
  writer batch (`Store.batch`), not `Store.write_finding`, which refuses finished runs (4.13 outcome rows are
  final; a finding is not an outcome row).
- With several roots a set lives under the first root that has its directory; a set directory found under none is
  missing, and a restore goes to the first root.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..records import Finding
from ..records import now as utc_now
from .backup import StoreWriter, copy_file, sha256_file, writer_call
from .health import EXIT_NOT_OPENED, NOT_OPENED_HINT
from .retention import LiveCopy, ensure_set_rows, live_copies, object_file, refresh_pins, run_of_set, ts
from .store import Store, StoreError, utc

SAMPLE_SHARE = 0.05
STATE_ORDER = ("missing", "corrupt", "ok")         # worst first
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditFailure:
    """A copy that was missing or corrupt, what was restored and the finding that records it."""
    sha256: str
    set_path: str
    state: str
    pinned: bool
    restored_from: str | None = None
    finding_id: str | None = None


@dataclass(frozen=True)
class AuditResult:
    """What one audit checked and found."""
    at: str
    pinned: int
    sampled: int
    failures: tuple[AuditFailure, ...] = ()

    @property
    def unrecorded(self) -> tuple[AuditFailure, ...]:
        """Failures no finding could be written for (no known run)."""
        return tuple(f for f in self.failures if f.finding_id is None)

    def health_rows(self) -> list[tuple[str, str]]:
        restored = sum(f.restored_from is not None for f in self.failures)
        return [("Artifact audit", f"{self.pinned + self.sampled} copies re-hashed ({self.pinned} pinned, "
                                   f"{self.sampled} sampled); {len(self.failures)} missing or corrupt; "
                                   f"{restored} restored from the backup mirror")]


def _roots(roots: str | os.PathLike | Sequence[str | os.PathLike]) -> list[Path]:
    if isinstance(roots, (str, os.PathLike)):
        return [Path(roots)]
    return [Path(r) for r in roots]


def _set_root(roots: list[Path], set_path: str) -> tuple[Path, bool]:
    """The directory of the set (the first root holding it, else under the first root) and whether it exists."""
    for root in roots:
        if (root / set_path).is_dir():
            return root / set_path, True
    return roots[0] / set_path, False


def check_copy(roots: list[Path], c: LiveCopy) -> str:
    """`ok`, `missing` or `corrupt` for one set copy (8.6)."""
    set_root, exists = _set_root(roots, c.set_path)
    path = object_file(set_root, c.sha256) if exists else None
    if path is None:
        return "missing"
    return "ok" if sha256_file(path) == c.sha256 else "corrupt"


def restore_copy(roots: list[Path], backup_root: Path, c: LiveCopy) -> Path | None:
    """Copy the mirror's copy of a pinned object back into its set when it verifies; returns the mirror file used,
    or None when the mirror has no good copy."""
    mirror = object_file(backup_root / c.set_path, c.sha256)
    if mirror is None or sha256_file(mirror) != c.sha256:
        return None
    set_root, _ = _set_root(roots, c.set_path)
    bad = object_file(set_root, c.sha256)
    if bad is not None:
        bad.unlink()                                # a corrupt copy is replaced, never trusted (8.4)
    copy_file(mirror, set_root / "objects" / c.sha256[:2] / mirror.name, verify=c.sha256)
    log.warning("artifact audit: restored %s of %s from the backup mirror %s", c.sha256, c.set_path, mirror)
    return mirror


def _attribution(store: Store, c: LiveCopy) -> tuple[str | None, str | None, int | None]:
    """(run, checkpoint, tier) of the finding for a failed copy (module docstring)."""
    run = run_of_set(c.set_path)
    named = ("(o.cand_capture_sha256 = :sha OR o.base_capture_sha256 = :sha OR o.heatmap_sha256 = :sha)"
             + (" AND o.run_id = :run" if run else ""))
    rows = store.query(f"SELECT o.run_id, o.checkpoint_id, c.tier FROM outcomes o JOIN checkpoints c ON "
                       f"c.checkpoint_id = o.checkpoint_id WHERE {named} ORDER BY c.tier, o.run_id, o.checkpoint_id "
                       "LIMIT 1", {"sha": c.sha256, "run": run})
    if rows:
        return rows[0]["run_id"], rows[0]["checkpoint_id"], rows[0]["tier"]
    rows = store.query("SELECT b.checkpoint_id, b.source_run_id, c.tier FROM baselines b JOIN checkpoints c ON "
                       "c.checkpoint_id = b.checkpoint_id WHERE b.image_sha256 = ?1 OR b.sidecar_sha256 = ?1 "
                       "ORDER BY c.tier, b.baseline_id LIMIT 1", (c.sha256,))
    cp, tier = (rows[0]["checkpoint_id"], rows[0]["tier"]) if rows else (None, None)
    first = store.query("SELECT first_run_id FROM artifacts WHERE sha256 = ?", (c.sha256,))
    for candidate in (run, rows[0]["source_run_id"] if rows else None, first[0][0] if first else None):
        if candidate and store.query("SELECT 1 FROM runs WHERE run_id = ?", (candidate,)):
            return candidate, cp, tier
    return None, cp, tier


def _finding_row(store: Store, c: LiveCopy, state: str, restored: Path | None, at: str) -> dict | None:
    """The `artifact_missing` findings row for a failed copy, or None when no run is known."""
    run, cp, tier = _attribution(store, c)
    if run is None:
        return None
    what = f"artifact {c.sha256} of {c.set_path} is {state} (8.6 weekly audit)"
    if restored is not None:
        what += "; restored from the backup mirror"
    evidence = {"artifacts": [c.sha256], "set_path": c.set_path, "audit_state": state, "pinned": c.pinned,
                "restored_from": None if restored is None else str(restored)}
    f = Finding(f"fnd-{run}-artifact-missing-{c.set_path}-{c.sha256}", run, "artifact_missing", what, run,
                checkpoint_id=cp, tier=tier, evidence=evidence, created_at=at)
    return {"natural_key": f.finding_id, "run_id": f.run_id, "checkpoint_id": f.checkpoint_id, "kind": f.kind,
            "tier": f.tier, "description": f.description, "evidence_json": json.dumps(f.evidence, sort_keys=True),
            "triage_state": f.triage_state, "synthetic": 0, "first_seen_run_id": f.first_seen_run_id,
            "created_at": utc(f.created_at)}


def _new_and_reopened(store: Store, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split this audit's finding rows into those to insert (no finding for the copy yet) and those whose finding
    is reopened with what was found now: it is no longer `open`, or it says something else (module docstring)."""
    new, reopen = [], []
    for row in rows:
        old = store.query("SELECT triage_state, description, evidence_json FROM findings WHERE natural_key = ?",
                          (row["natural_key"],))
        if not old:
            new.append(row)
        elif (old[0]["triage_state"], old[0]["description"], old[0]["evidence_json"]) != (
                "open", row["description"], row["evidence_json"]):
            reopen.append(row)
    return new, reopen


def _copies(store: Store) -> list[LiveCopy]:
    """The audit's first writer unit: pins and set rows brought up to date, then the copies to audit."""
    refresh_pins(store)
    ensure_set_rows(store)
    return [c for c in live_copies(store) if not c.quarantined]


def _record(store: Store, checked: list[tuple[LiveCopy, str, Path | None]], at: str) -> list[AuditFailure]:
    """The audit's last writer unit: `audit_state`, the findings and the restores of the checked copies (each with
    what was found and the mirror file it was restored from), in one batch, then `last_artifact_audit_at`."""
    states: dict[str, list[str]] = {}
    failures: list[AuditFailure] = []
    rows: list[dict] = []
    restores: list[dict] = []
    for c, found, restored in checked:
        states.setdefault(c.sha256, []).append("ok" if restored is not None else found)
        if found == "ok":
            continue
        if restored is not None:
            restores.append({"op": "restore", "sha256": c.sha256, "set_path": c.set_path, "from": str(restored)})
        row = _finding_row(store, c, found, restored, at)
        if row is not None:
            rows.append(row)
        failures.append(AuditFailure(c.sha256, c.set_path, found, c.pinned,
                                     None if restored is None else str(restored),
                                     None if row is None else row["natural_key"]))
    new, reopen = _new_and_reopened(store, rows)
    with store.batch("artifact_audit") as b:
        for sha, seen in sorted(states.items()):
            b.update("artifacts", {"sha256": sha}, {"audit_state": min(seen, key=STATE_ORDER.index),
                                                     "last_audit_at": at})
        for row in new:
            b.insert("findings", row)
        for row in reopen:
            b.update("findings", {"natural_key": row["natural_key"]}, {
                "triage_state": "open", "closed_at": None, "description": row["description"],
                "evidence_json": row["evidence_json"]})
        b.rows.extend(restores)
    store.set_system_state("last_artifact_audit_at", at, at=at)
    return failures


def run_audit(store: Store | StoreWriter, roots: str | os.PathLike | Sequence[str | os.PathLike],
              backup_root: str | os.PathLike, rng: random.Random, now: str) -> AuditResult:
    """The weekly audit (module docstring). `store` is the Store this process writes or, while the Reporter service
    runs, its writer (`store.backup.writer_call`, 8.2): the writer is held only to read the copies and to record
    the result, while the re-hashing and the restores run on the caller's thread."""
    write = writer_call(store)
    at, dirs, mirror = utc(now), _roots(roots), Path(backup_root)
    copies = write(_copies)
    pinned = [c for c in copies if c.pinned]
    rest = [c for c in copies if not c.pinned]
    sample = rng.sample(rest, math.ceil(len(rest) * SAMPLE_SHARE))
    checked = []
    for c in pinned + sample:
        found = check_copy(dirs, c)
        checked.append((c, found, restore_copy(dirs, mirror, c) if found != "ok" and c.pinned else None))
    failures = write(lambda s: _record(s, checked, at))
    return AuditResult(at, len(pinned), len(sample), tuple(failures))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sentinel audit", description="The weekly artifact audit (8.6).")
    p.add_argument("--db", required=True, help="the Central Store database")
    p.add_argument("--artifact-root", required=True, action="append", help="an artifact root (8.4); repeatable")
    p.add_argument("--backup-root", required=True, help="the backup location holding the mirror")
    p.add_argument("--seed", type=int, help="the sample seed (default: the ISO year and week of --now)")
    p.add_argument("--now", help="the audit time (default now)")
    return p


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when every checked copy is ok, 1 when any is missing or corrupt (listed on stdout), 2 on a usage
    error or a missing database, 4 (`store.health.EXIT_NOT_OPENED`) when the Central Store would not open for
    writing (the Reporter service holds writer.lock: the service runs the audit through its writer, 8.2)."""
    a = _parser().parse_args(argv)
    if not Path(a.db).is_file():
        print(f"sentinel audit: no database at {a.db}", file=sys.stderr)
        return 2
    when = utc(a.now) or utc_now()
    year, week, _ = ts(when).isocalendar()
    try:
        store = Store(a.db)
    except StoreError as e:
        print(f"sentinel audit: not run: {e}; {NOT_OPENED_HINT}", file=sys.stderr)
        return EXIT_NOT_OPENED
    with store:
        res = run_audit(store, a.artifact_root, a.backup_root,
                        random.Random(a.seed if a.seed is not None else year * 100 + week), when)
    for label, text in res.health_rows():
        print(f"{label}: {text}")
    for f in res.failures:
        print(f"{f.state} {f.set_path} {f.sha256}" + (f" restored from {f.restored_from}" if f.restored_from else "")
              + (f" finding {f.finding_id}" if f.finding_id else " (no run: no finding written)"))
    return 1 if res.failures else 0


if __name__ == "__main__":
    sys.exit(main())
