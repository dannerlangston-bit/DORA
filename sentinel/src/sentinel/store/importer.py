"""The shard import of the Reporter service, lost and re-queued rigs (8.2, 8.4, 8.9, 10.7).

At `reported` the Rig Manager ships the shard directory (8.4 RIG layout: run.db, results.jsonl, the two journals,
manifest.json, objects/) to `incoming/<shard_id>/` on the control host. `import_shard(central, incoming_dir,
run_id, rehash_request, artifact_root=)` then:

1. marks a live shard `shipping`, hashes the Run Store and the ledger (`run_store_sha256`, `ledger_sha256`,
   `ledger_rows`) and reads the shard id and attempt from the Run Store;
2. verifies every object: each file under `objects/` hashes to its name, and every hash the Run Store's artifacts
   rows and the manifest name is there. A mismatch is re-requested from the rig through `rehash_request(sha256,
   attempt)` (the rig's `PUT /v1/artifacts/{sha256}` again) up to 3 times; then the shard is `import_failed`
   with `upload_hash_mismatch` and the run `aborted_harness (reason: artifact_persist_failed)` (8.4, 8.9);
3. verifies that the Run Store's rows equal the ledger: the ledger is replayed into a scratch database
   (`Store.replay`) and every table compared. A difference is `import_failed` and `aborted_harness (reason:
   import_conflict)` with nothing imported (8.9);
4. moves the objects into `runs/<run_id>/objects/`, keeps the ledger and journals verbatim under
   `runs/<run_id>/shards/<n>/`, merges the manifest into `runs/<run_id>/manifest.json`, and writes every row in
   one transaction (`Store.import_shard_rows`); the shard is `imported` only when both halves succeed, and the
   files go back to `incoming` when the transaction fails (8.2: a partially imported shard is never visible);
5. a Central Store that cannot commit (`StoreWriteFailed`) leaves the shard `shipping` and retries with backoff
   (8.9); after the last retry the error propagates and the dead-man covers the run.

Lost rigs (8.9, 10.7): `mark_shard_lost` when the heartbeat is gone past the deadline; `mark_shard_superseded`
re-queues it once to another rig as the next attempt; `abort_dead_man` ends a run whose rig stayed away as
`aborted_harness (reason: dead_man)` with "rig unreachable, run data intact on rig". When the rig returns, the same
`import_shard` keeps an older attempt's rows as a superseded attempt (`outcomes_superseded`, never counted) and
imports a lost shard of a dead-man run into it, re-evaluating the run (`Store.import_shard_rows`).
`re_evaluate_run` writes the run outcome as the worst of the shards' once every shard is settled (8.2).

Choices where the spec is silent or ambiguous (reported):
- A re-queued shard keeps its `shards` row (`shard_id` is the key, `attempt` counts, 8.3 comment): the 8.3 run
  trigger refuses `complete` while any shard row is not `imported`, so a separate row for the lost attempt would
  keep the run from ever being complete. The `superseded` shard state is therefore not written; the superseded
  attempt is the Run Store whose attempt is older than the row's.
- The shard's scope for `orchestrator_restart_budget` is the Run Store's own `run_scope` (the Orchestrator begins
  the run on its rig with its shard's checkpoints); the Central Store keeps the whole run's scope.
- `ledger_rows` counts the ledger's row entries (8.4: "append-only ledger of every database row"); the hashes are
  over the files as shipped.
- When the Run Store and the ledger differ only by the ledger's last batch (the Orchestrator appended it and died
  before its commit), the ledger's rows are imported (8.9: "the ledger already holds the rows for replay");
  any other difference is an import conflict.
- The journals in the shard directory are ingested with the shard (`store.journal`): a shard shipped "as it is"
  after the restart budget was never resumed, and a journal already ingested on resume adds nothing (8.2).
- A superseded attempt's files go to `runs/<run_id>/shards/<n>/attempt-<k>/`, its objects into the run's set
  (indexed, so its rows' evidence can be shown), and nothing of it into the run's manifest. A superseded attempt
  that fails verification is left in `incoming` and the run is not touched.
- An import failure ends the run only once every shard is settled (`re_evaluate_run`); the shard's outcome here is
  `aborted_harness` with the failure's reason, so the run can never end less severe (8.2 worst of the shards).
- A shard id `<run_id>/<n>` is shipped to `incoming/<run_id>/<n>/`; the directory is passed in.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..records import RunOutcome
from . import journal
from .artifacts import MANIFEST, SHA256_RE
from .store import (ImportConflict, ShardRows, Store, StoreError, StoreRefused, StoreWriteFailed, read_ledger,
                    utc)

RUN_DB = "run.db"
LEDGER = "results.jsonl"
REREQUESTS = 3                       # 8.4, 8.9: an upload hash mismatch is re-requested up to three times
COMMIT_RETRIES = 3                   # 8.9: a Central Store commit failure retries with backoff
LOST_DETAIL = "rig unreachable, run data intact on rig"   # 8.9
_SKIP_TABLES = ("outcome_reasons", "system_state")       # seeded at open, not by the ledger

RehashRequest = Callable[[str, int], None]


@dataclass(frozen=True)
class ImportResult:
    """What one `import_shard` did. `state` is `imported`, `lost_returned` (a lost shard of a dead-man run, the
    run re-evaluated), `superseded` (an older attempt kept for diagnosis), `replayed` (already imported) or
    `import_failed`; `reason` is `import_conflict` or `upload_hash_mismatch` for a failure, and for a superseded
    attempt that failed verification (nothing written then). `moved` lists the central paths of the objects this
    import renamed into the run's set."""
    shard_id: str
    run_id: str
    attempt: int
    state: str
    reason: str | None = None
    detail: str | None = None
    rerequests: int = 0
    run_store_sha256: str | None = None
    ledger_sha256: str | None = None
    ledger_rows: int = 0
    run_outcome: RunOutcome | None = None
    moved: tuple[str, ...] = field(default=(), repr=False)


class _Failed(Exception):
    """A verification failure: (reason, run abort reason, detail)."""

    def __init__(self, reason: str, abort: str, detail: str, rerequests: int = 0):
        super().__init__(detail)
        self.reason, self.abort, self.detail, self.rerequests = reason, abort, detail, rerequests


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _objects(inc: Path) -> dict[str, Path]:
    """Every object file under `incoming/<shard>/objects/`, by the hash its name claims."""
    out = {}
    root = inc / "objects"
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            name = p.name.split(".", 1)[0]
            if p.is_file() and SHA256_RE.match(name):
                out[name] = p
    return out


def _named(inc: Path, reader: Store) -> set[str]:
    """The hashes the Run Store's artifacts rows and the shard manifest name."""
    names = {r[0] for r in reader.conn.execute("SELECT sha256 FROM artifacts")}
    mf = inc / MANIFEST
    if mf.exists():
        names |= {e["sha256"] for e in json.loads(mf.read_text(encoding="utf-8")).get("entries", [])}
    return names


def verify_objects(inc: Path, named: set[str], rehash_request: RehashRequest | None) -> tuple[dict[str, Path], int]:
    """Step 2 of the module docstring: (the verified objects by hash, the re-requests made). Raises _Failed with
    upload_hash_mismatch when a mismatch persists after `REREQUESTS` re-requests (or with no way to ask)."""
    def bad() -> list[str]:
        objs = _objects(inc)
        return sorted(h for h in set(objs) | named if h not in objs or _sha256(objs[h]) != h)

    wrong, asked = bad(), 0
    while wrong and asked < REREQUESTS and rehash_request is not None:
        asked += 1
        for sha in wrong:
            rehash_request(sha, asked)
        wrong = bad()
    if wrong:
        raise _Failed("upload_hash_mismatch", "artifact_persist_failed",
                      f"upload_hash_mismatch: {wrong[0]} ({len(wrong)} object(s)) after {asked} re-request(s)", asked)
    return _objects(inc), asked


def _tables(path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        names = [n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE "
                                            "'sqlite_%' ORDER BY name") if n not in _SKIP_TABLES]
        return {n: sorted(conn.execute(f"SELECT * FROM {n}").fetchall(), key=repr) for n in names}
    finally:
        conn.close()


def ledger_differences(run_db: Path, image: Path) -> list[str]:
    """The tables whose rows differ between a Run Store and a ledger image (8.2), with counts; empty when equal."""
    a, b = _tables(run_db), _tables(image)
    out = []
    for table in sorted(set(a) | set(b)):
        ra, rb = a.get(table, []), b.get(table, [])
        if ra != rb:
            out.append(f"{table}: {len(set(ra) - set(rb))} row(s) only in the Run Store, {len(set(rb) - set(ra))} "
                       "only in the ledger")
    return out


def _image(ledger: Path, dest: Path, central: Store, lines: list[str] | None = None) -> Path:
    """Replay `ledger` (or just `lines` of it) into a fresh scratch database at `dest`."""
    src = ledger
    if lines is not None:
        src = dest.with_suffix(".jsonl")
        src.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
    with Store(dest, taxonomy=central.taxonomy, ledger=False, require_local=False) as img:
        img.replay(src)
    return dest


def verify_ledger(run_db: Path, ledger: Path, central: Store, scratch: Path) -> Path:
    """Step 3 of the module docstring: the database whose rows to import (the Run Store, or the ledger's image
    when only the uncommitted last batch is missing from the Run Store). Raises _Failed with import_conflict."""
    lines = [line for _, line in read_ledger(ledger)]
    try:
        full = _image(ledger, scratch / "image.db", central)
        diffs = ledger_differences(run_db, full)
        if not diffs:
            return run_db
        if lines and not ledger_differences(run_db, _image(ledger, scratch / "image-1.db", central, lines[:-1])):
            return full
    except (StoreError, sqlite3.DatabaseError) as e:
        raise _Failed("import_conflict", "import_conflict", f"the ledger does not replay: {e}") from e
    raise _Failed("import_conflict", "import_conflict", "the Run Store's rows disagree with the ledger: "
                  + "; ".join(diffs))


def _shard_number(shard_id: str) -> str:
    return shard_id.rsplit("/", 1)[-1]


class _Placement:
    """The files an import puts into the central tree, undone when its transaction fails (8.2)."""

    def __init__(self) -> None:
        self.moves: list[tuple[Path, Path]] = []           # (incoming, central) of objects moved here
        self.created: list[Path] = []
        self.restore: list[tuple[Path, bytes | None]] = []  # files replaced, with their old content

    def move(self, src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():                                   # the same object from another shard of the run
            return
        os.replace(src, dest)
        self.moves.append((src, dest))

    def write(self, dest: Path, data: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.restore.append((dest, dest.read_bytes() if dest.exists() else None))
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)

    def undo(self) -> None:
        for src, dest in reversed(self.moves):
            os.replace(dest, src)
        for dest, old in reversed(self.restore):
            if old is None:
                dest.unlink(missing_ok=True)
            else:
                dest.write_bytes(old)
        self.moves.clear()
        self.restore.clear()


def _merged_manifest(run_dir: Path, inc: Path, shard_id: str) -> bytes | None:
    """The run's manifest with the shard's entries added (8.4: the union of the shards' manifests); a name that
    another shard maps to another hash is an ImportConflict."""
    mf = inc / MANIFEST
    if not mf.exists():
        return None
    shard = json.loads(mf.read_text(encoding="utf-8"))
    out = run_dir / MANIFEST
    run = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"header": {}, "entries": []}
    entries = {e["name"]: e for e in run.get("entries", [])}
    for e in shard.get("entries", []):
        old = entries.get(e["name"])
        if old is not None and old["sha256"] != e["sha256"]:
            raise ImportConflict(f"manifest name {e['name']} maps to {old['sha256']} in the run and {e['sha256']} in "
                                 f"{shard_id} (8.4)")
        entries[e["name"]] = e
    header = {k: v for k, v in (shard.get("header") or {}).items() if k != "shard_id"}
    header = {**header, **run.get("header", {})}
    header["shards"] = sorted(set(header.get("shards", [])) | {shard_id})
    body = {"header": header, "entries": [entries[k] for k in sorted(entries)]}
    return json.dumps(body, indent=1, sort_keys=True).encode()


def _place(inc: Path, run_dir: Path, shard_id: str, attempt: int, objects: dict[str, Path], *,
           superseded: bool) -> tuple[_Placement, dict[str, Path]]:
    """Step 4's files: objects into the run's set, the ledger and journals kept verbatim, the manifest merged."""
    placed = _Placement()
    central = {sha: run_dir / "objects" / sha[:2] / p.name for sha, p in objects.items()}
    keep = run_dir / "shards" / _shard_number(shard_id)
    if superseded:
        keep = keep / f"attempt-{attempt}"
    try:
        merged = None if superseded else _merged_manifest(run_dir, inc, shard_id)
        for sha, p in objects.items():
            placed.move(p, central[sha])
        for name in (LEDGER, journal.GATEWAY_JOURNAL, journal.WATCHDOG_JOURNAL, *((MANIFEST,) if superseded else ())):
            if (inc / name).exists():
                placed.write(keep / name, (inc / name).read_bytes())
        if merged is not None:
            placed.write(run_dir / MANIFEST, merged)
    except BaseException:
        placed.undo()
        raise
    return placed, central


def _journals(inc: Path, run_id: str, shard_id: str) -> tuple[list, list]:
    gateway = journal.read_journal(inc / journal.GATEWAY_JOURNAL, "gateway_hook", run_id=run_id, shard_id=shard_id)
    watchdog = journal.read_journal(inc / journal.WATCHDOG_JOURNAL, "watchdog", run_id=run_id, shard_id=shard_id)
    return gateway, watchdog


def import_shard(central: Store, incoming_dir: str | os.PathLike, run_id: str,
                 rehash_request: RehashRequest | None = None, *, artifact_root: str | os.PathLike,
                 restart_budget_spent: bool = False, at: str | None = None, retries: int = COMMIT_RETRIES,
                 backoff_s: float = 1.0, sleep: Callable[[float], None] = time.sleep) -> ImportResult:
    """Import one shipped shard directory into the Central Store (module docstring). `restart_budget_spent` is the
    Rig Manager's flag for a shard shipped as it was after the Orchestrator's restart budget was spent (8.2).
    Raises StoreWriteFailed after `retries` commit failures (the shard stays `shipping`), StoreRefused for a shard
    directory that is not of `run_id`; every verification failure is an `import_failed` result."""
    inc = Path(incoming_dir)
    db, ledger = inc / RUN_DB, inc / LEDGER
    if not db.exists():
        raise StoreRefused(f"{inc} holds no {RUN_DB} (8.4)")
    reader = Store.reader(db, taxonomy=central.taxonomy)
    try:
        shards = reader.conn.execute("SELECT shard_id, run_id, attempt FROM shards").fetchall()
        named = _named(inc, reader)
    finally:
        reader.close()
    if len(shards) != 1 or shards[0]["run_id"] != run_id:
        raise StoreRefused(f"{db} is not the Run Store of one shard of run {run_id} (8.1)")
    shard_id, attempt = shards[0]["shard_id"], shards[0]["attempt"]
    row = central.query("SELECT state, attempt FROM shards WHERE shard_id = ? AND run_id = ?", (shard_id, run_id))
    if not row:
        raise StoreRefused(f"{shard_id} is not a shard of run {run_id} in the Central Store (8.3)")
    superseded = attempt < row[0]["attempt"]
    db_sha = _sha256(db)
    if row[0]["state"] == "imported" and not superseded and central.query(
            "SELECT 1 FROM shards WHERE shard_id = ? AND run_store_sha256 = ?", (shard_id, db_sha)):
        return ImportResult(shard_id=shard_id, run_id=run_id, attempt=attempt, state="replayed",
                            run_store_sha256=db_sha)          # 8.2: a rig retrying after a dropped connection
    if row[0]["state"] in ("queued", "running", "finished"):
        central.mark_shard_shipping(shard_id)
    hashes = {"run_store_sha256": db_sha, "ledger_sha256": _sha256(ledger) if ledger.exists() else None,
              "ledger_rows": sum(len(b.get("rows") or ()) for b, _ in read_ledger(ledger))}
    base = dict(shard_id=shard_id, run_id=run_id, attempt=attempt, **hashes)
    asked = 0
    try:
        objects, asked = verify_objects(inc, named, rehash_request)
        gateway, watchdog = ([], []) if superseded else _journals(inc, run_id, shard_id)
        with tempfile.TemporaryDirectory(prefix="sentinel-import-") as scratch:
            source = verify_ledger(db, ledger, central, Path(scratch)) if ledger.exists() else db
            rows, placed = _load(central, inc, source, shard_id, run_id, attempt, objects, Path(artifact_root),
                                 superseded=superseded)
    except _Failed as f:
        return _fail(central, base, f, asked or f.rerequests, superseded=superseded)
    except StoreRefused as e:                         # a conflicting row, manifest name or journal line
        return _fail(central, base, _Failed("import_conflict", "import_conflict", str(e)), asked,
                     superseded=superseded)
    for n in range(retries + 1):
        try:
            state = central.import_shard_rows(rows, restart_budget_spent=restart_budget_spent, journal=gateway,
                                              keyed=watchdog, hashes=hashes, at=at)
            break
        except StoreWriteFailed:
            if n == retries:
                placed.undo()
                raise
            sleep(backoff_s * 2 ** n)
        except StoreRefused as e:
            placed.undo()
            if isinstance(e, ImportConflict) or not superseded:
                return _fail(central, base, _Failed("import_conflict", "import_conflict", str(e)), asked,
                             superseded=superseded)
            raise
    outcome = central.run_outcome(run_id) if state == "lost_returned" else None
    return ImportResult(**base, state=state, rerequests=asked, run_outcome=outcome,
                        moved=tuple(str(d) for _, d in placed.moves))


def _load(central: Store, inc: Path, source: Path, shard_id: str, run_id: str, attempt: int,
          objects: dict[str, Path], artifact_root: Path, *, superseded: bool) -> tuple[ShardRows, _Placement]:
    """Read the shard's rows from `source` and place its files (step 4 before the transaction)."""
    run_dir = artifact_root / "runs" / run_id
    placed, paths = _place(inc, run_dir, shard_id, attempt, objects, superseded=superseded)
    try:
        reader = Store.reader(source, taxonomy=central.taxonomy)
        try:
            rows = reader.shard_rows(shard_id, objects=paths, set_path=f"runs/{run_id}")
        finally:
            reader.close()
    except BaseException:
        placed.undo()
        raise
    return rows, placed


def _fail(central: Store, base: dict, f: _Failed, asked: int, *, superseded: bool) -> ImportResult:
    """8.9: the shard is `import_failed`, its outcome `aborted_harness` with the failure's reason and run health
    says why; the run ends once every shard is settled. A superseded attempt only reports the failure."""
    if superseded:
        return ImportResult(**base, state="superseded", reason=f.reason, detail=f.detail, rerequests=asked)
    if central.query("SELECT 1 FROM shards WHERE shard_id = ? AND state = 'import_failed'", (base["shard_id"],)):
        return ImportResult(**base, state="import_failed", reason=f.reason, detail=f.detail, rerequests=asked,
                            run_outcome=central.run_outcome(base["run_id"]))      # it failed before: no change
    central.mark_import_failed(base["shard_id"], f.abort, f.detail)
    health = central.run_summary(base["run_id"]).get("health", {})
    failed = [*health.get("import_failed", []), {"shard_id": base["shard_id"], "reason": f.reason,
                                                 "detail": f.detail}]
    central.update_summary(base["run_id"], {"health": {**health, "import_failed": failed}})
    return ImportResult(**base, state="import_failed", reason=f.reason, detail=f.detail, rerequests=asked,
                        run_outcome=central.settle_run(base["run_id"]))


def mark_shard_lost(central: Store, shard_id: str) -> None:
    """The rig of `shard_id` is unreachable past the deadline (8.9, 10.7): the shard is `lost`."""
    central.mark_shard_lost(shard_id)


def mark_shard_superseded(central: Store, shard_id: str, *, rig_id: str, at: str | None = None) -> int:
    """10.7: re-queue a lost shard once, in full, to `rig_id`; the lost attempt is superseded (module docstring).
    Returns the new attempt number, which the new rig's Run Store carries."""
    return central.requeue_shard(shard_id, rig_id=rig_id, at=at)


def abort_dead_man(central: Store, run_id: str, *, at: str | None = None) -> RunOutcome:
    """8.9 shard lost, dead-man deadline passed: every checkpoint without an outcome is `harness_error (reason:
    dead_man)` and the run `aborted_harness (reason: dead_man)` with "rig unreachable, run data intact on rig"
    for each lost shard in run health. The rows are imported when the rig returns (`import_shard`)."""
    lost = [r[0] for r in central.query("SELECT shard_id FROM shards WHERE run_id = ? AND state = 'lost' ORDER BY "
                                        "shard_id", (run_id,))]
    central.fill_unreached(run_id, "harness_error", "dead_man")
    run_type = central.query("SELECT run_type FROM runs WHERE run_id = ?", (run_id,))[0][0]
    health = central.run_summary(run_id).get("health", {})
    summary = {"health": {**health, "lost_shards": [{"shard_id": s, "detail": LOST_DETAIL} for s in lost]}}
    return central.end_run(RunOutcome(run_id=run_id, run_type=run_type, status="aborted_harness",
                                      last_gate="running", reason="dead_man", ended_at=utc(at)), summary=summary)


def re_evaluate_run(central: Store, run_id: str, *, at: str | None = None) -> RunOutcome | None:
    """8.2: the run outcome, the worst of the shards', once every shard is imported or import_failed (None before);
    `Store.settle_run`."""
    return central.settle_run(run_id, at=at)
