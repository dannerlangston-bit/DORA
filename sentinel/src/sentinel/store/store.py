"""The SQLite store of 8.1 to 8.3: one writer, the 8.3 schema generated from taxonomy.yaml, the 4.13 later changes,
the 4.14 decision lookup and inheritance, the 8.6 retention safety rules and the 8.9 failure modes.

`Store(path)` is the single writer (8.2) of a Run Store or the Central Store; both have the same schema.
`connect_reader(path)` opens the read-only connection 8.2 gives every other process. Records go in and come out as
`sentinel.records` types; the few inputs records has no type for (run identity, scope, reviews) are the small
frozen dataclasses below.

Enforced here and, where SQL can say it, again by the schema:
- one writer process: a `writer.lock` beside the database holds the writer's PID and a second writer refuses to
  start while that PID is alive (8.2); a stale lock (dead PID) is taken over. The connection is bound to the
  thread that opened it, which is the "one writer thread" of 8.2.
- the database and the artifact root must be on a local volume: UNC paths, and on POSIX the network file systems
  of /proc/mounts, are refused; on Windows the drive must be DRIVE_FIXED (8.2, H7, 8.9).
- every outcome, reason, gate, finding kind, triage state and decision value is in the closed taxonomy: records
  validate at construction, the store re-validates (records are mutable), and the generated CHECKs refuse the rest.
- outcomes are written once (4.13, 8.2). A replay of the same record is a no-op; a different record for the same
  (run, checkpoint) is an `ImportConflict`. The only later changes are the three of 4.13: `set_decision_ref`,
  `apply_review` (pending -> done with the verdict's outcome, or as an annotation only when a human decision
  already exists on the item, F28) and `end_review` (reviewer_cap or reviewer_unavailable, no review id).
- decisions are human-only (4.14, L-DEC-01) and never edited in place: supersede and revoke only (8.3).
- a run is `complete` only with an outcome for every scoped checkpoint, no env_error, no store-failure
  harness_error, no unimported shard and every outcome-named artifact stored (8.1, 8.3, 8.9). An outcome that
  names an artifact the store has no row for is refused at write (8.2).
- every write batch is one `BEGIN IMMEDIATE` transaction, appended to `results.jsonl` before it commits (8.2).

Choices where the spec is ambiguous or silent (reported to the coordinator):
- Additions to the 8.3 DDL, at the end of schema.sql: `run_scope` (the scoped checkpoint ids with the persona
  of the visit; 8.3 keeps only `runs.scope_count`, which cannot say which checkpoint is missing),
  `outcome_records` (the 4.13 record as written, so the store returns persona, classification and diff regions,
  which 8.3 has no column for), and triggers for F65 (an env_error names a gate from taxonomy.yaml), for "nothing
  else is ever rewritten" (every outcome column the 8.3 triggers leave open, and deletion), for decision_ref
  set once, review_id only with the review transition, and complete-needs-every-scoped-id.
- outcome_reasons seed: taxonomy.yaml has no owner_section or detail_means, so rows carry owner_section '4.13'
  (the reason vocabulary table the file is generated from; '1.10' for rows inherited through
  `reason_inheritance`, as 8.3's INSERT ... SELECT writes) and detail_means is the parameter placeholder
  (`<step>`). A reason is parametric when its taxonomy pattern has a placeholder. So `fingerprint_mismatch`,
  parametric in 8.3's hand-written seed but plain in taxonomy.yaml, is not parametric here.
- At service start the seed is synced (8.3: "seeded ... at service start"): new reasons are added, reasons no
  longer in the file are retired (active = 0), never deleted. A taxonomy change that alters a generated CHECK
  (any list) is refused at open: that needs a migration, and none exists yet.
- `reason_detail` holds the reason's parameter for a parametric reason and the record's `detail` otherwise; with
  both, `<param>\\n<detail>`. For invariant_violation without a detail it holds the fired invariant ids (8.3).
- Decision ids are `dec-<zero-padded sequence>` (4.14) and map to the integer `decisions.decision_id`; finding ids
  are the findings' `natural_key`. The stored `hamming_tolerance` is the effective one (4.14): the asked value,
  clamped to 6 on a tier-1 checkpoint ("tier-1 checkpoint, so the override applies"; records.Decision defaults to
  8 and cannot say whether 8 was asked for) and to the region's calibrated tolerance at the run's config commit.
  noise_signature and request_mask get `expires_at` = decided_at + 30 days when none is given (8.3, 4.14).
- Step outcomes become `step_events` rows with source orchestrator, action `step`, gate_decision none (records
  has no gateway event), mode `agent` for an agent rung or fallback, and `finding:<ref>` in `reason`.
- Run counts exclude synthetic rows (canary mutants are not checkpoints of the run, 10.3).
- A finished run is not re-ended with a different outcome (the 10.7 re-evaluation of a returning rig belongs
  to the import, which is not this module).
- Timestamps are normalised to UTC ISO 8601 seconds on write (8.3).
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..records import (ACTION_VERDICT, DATA_MODES, PERSONAS, RUN_TYPES, CheckpointOutcome, Classification, Component,
                       Decision, DiffSummary, Finding, RegionDiff, RunOutcome, Signature, StepOutcome, now,
                       to_jsonable)
from ..taxonomy import Taxonomy, TaxonomyError, checkpoint_parts, load
from .artifacts import ArtifactStore, StoredArtifact

SCHEMA_VERSION = 1
DEFAULT_TOLERANCE = 8          # 4.14, 9.7: bits of 64
TIER1_TOLERANCE = 6
RELATED_LIMIT = 12             # 4.14: distances above the tolerance and up to 12 are shown, never inherited
EXPIRY_DAYS = 30               # 8.3: noise_signature and request_mask default expiry
# 4.14: the one action whose stored verdict depends on the case; every other action maps through ACTION_VERDICT.
MULTI_VERDICT = {"reject_baseline": ("confirmed_bug", "noise")}
# 4.14 table: the finding triage state an action leaves.
TRIAGE_FOR_ACTION = {
    "confirm_bug": "confirmed_bug", "mark_intended": "intended", "request_mask": "open",
    "noise_signature": "noise", "agent_mistake": "agent_mistake", "expected_behavior": "expected_behavior",
    "approve_baseline": "closed", "fixed_in_build": "fixed_pending_verify",
}
# L-DEC-01 and the 8.3 CHECKs: identities that are never a human.
SERVICE_IDENTITIES = frozenset({"", "ai", "reviewer", "system", "orchestrator", "reporter"})
STORE_FAILURE_REASONS = ("store_write_failed", "artifact_persist_failed", "redaction_version_missing")
PENDING_REASONS = ("reviewer_unavailable", "global_shift")      # 4.13: pending only while queued with these
LOCATOR_LEVELS = ("jab", "image", "ocr", "agent")
NETWORK_FS = frozenset({"nfs", "nfs4", "cifs", "smb3", "smbfs", "afs", "ncpfs", "fuse.sshfs"})
_PHASH_RE = re.compile(r"^[0-9a-f]{16}$")
_DECISION_RE = re.compile(r"^dec-(\d+)$")
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)(?::([^}]*))?\}\}")


# ---------------------------------------------------------------------------------------------------------------
# errors

class StoreError(Exception):
    """The store cannot open or cannot do what was asked."""


class WriterLockError(StoreError):
    """Another live process holds the writer lock (8.2)."""


class StoreRefused(StoreError, ValueError):
    """A write broke a rule of 4.13, 4.14 or 8.3; nothing was written (8.9: refused with the reason)."""


class ImportConflict(StoreRefused):
    """A row with the same natural key exists and differs (8.2, 8.9: import_conflict); never overwritten."""


class RunNotComplete(StoreRefused):
    """`complete` was asked for a run that cannot be complete (8.1, 8.3 run trigger)."""


class StoreWriteFailed(StoreError):
    """The writer could not commit (busy after retries, I/O error, disk full): 8.9 store_write_failed."""


# ---------------------------------------------------------------------------------------------------------------
# inputs records has no type for

@dataclass(frozen=True)
class ScopeEntry:
    """One checkpoint a run is asked to visit: the 8.3 `checkpoints` row plus the persona of the visit."""
    checkpoint_id: str
    workspace_id: str
    persona: str
    tier: int
    data_mode: str
    flow_class: str | None = None          # flow checkpoints only (8.3 checkpoints.flow_class)
    recapturable: bool = True


@dataclass(frozen=True)
class ShardPlan:
    """One shard of a run (8.3 `shards`). The rig must be registered first."""
    shard_id: str                          # <run_id>/<n>
    rig_id: str
    sides: str = "both"
    scope_count: int | None = None         # None: the whole run scope (a one-shard run)
    attempt: int = 1


@dataclass(frozen=True)
class RunPlan:
    """What `begin_run` needs: the 8.3 `runs` identity columns and the scope (2.11)."""
    run_id: str
    run_type: str
    config_commit: str
    config_tarball_sha256: str
    snapshot_set_sha256: str
    harness_sha: str
    authorization_ref: str
    budget_sec: int
    scope: tuple[ScopeEntry, ...]
    shards: tuple[ShardPlan, ...] = ()
    pair_id: int | None = None
    manifest_sha256: str | None = None
    pr_ref: str | None = None
    pr_author: str | None = None
    started_at: str = field(default_factory=now)


@dataclass(frozen=True)
class Review:
    """A Reviewer verdict for the 8.3 `reviews` table (3.13 output with its provenance)."""
    run_id: str
    checkpoint_id: str
    diff_hash: str
    model_id: str
    prompt_hash: str
    request_sha256: str
    response_json: str
    verdict: str
    mapped_outcome: str
    confidence: float
    region_id: str | None = None
    fewshot_version: str | None = None
    redaction_version: str | None = None
    mask_version: str | None = None
    votes: int = 1
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    batch_id: str | None = None
    created_at: str = field(default_factory=now)


@dataclass(frozen=True)
class DecisionMatch:
    decision: Decision
    distance: int


@dataclass(frozen=True)
class DecisionLookup:
    """The answer of the 8.3 signature lookup: the full verdict set (F25, 9.7)."""
    matches: tuple[DecisionMatch, ...] = ()       # every active decision within tolerance, nearest then newest
    related: tuple[DecisionMatch, ...] = ()       # above tolerance, up to 12 bits: shown, never inherited (4.14)

    @property
    def verdicts(self) -> frozenset[str]:
        return frozenset(m.decision.verdict for m in self.matches)

    @property
    def conflict(self) -> bool:
        """Verdicts differ: nothing is inherited and the finding lists both decisions (9.7)."""
        return len(self.verdicts) > 1

    @property
    def inherited(self) -> Decision | None:
        """The nearest, then newest, match when every match carries one verdict; else None (F25)."""
        if not self.matches or self.conflict:
            return None
        return self.matches[0].decision


@dataclass(frozen=True)
class SweepResult:
    """What one retention sweep did (8.6)."""
    deleted: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    skipped: bool = False                         # "retention skipped: backup failed"


# ---------------------------------------------------------------------------------------------------------------
# small helpers

def utc(ts: str | None) -> str | None:
    """ISO 8601 UTC text to the second (8.3); a naive time is taken as UTC."""
    if ts is None:
        return None
    d = datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def phash_bytes(p: bytes | str) -> bytes:
    """A 64-bit pHash as the 8-byte blob 8.3 stores; accepts the blob or 16 hex characters (L-DEC-03)."""
    if isinstance(p, (bytes, bytearray, memoryview)):
        b = bytes(p)
    else:
        s = str(p).lower()
        if not _PHASH_RE.match(s):
            raise StoreRefused(f"phash must be 16 hex characters, got {p!r} (L-DEC-03)")
        b = bytes.fromhex(s)
    if len(b) != 8:
        raise StoreRefused("phash must be 8 bytes (8.3)")
    return b


def hamming(a: bytes | str | None, b: bytes | str | None) -> int | None:
    """Population count of the XOR of two 8-byte pHashes: the SQL function 8.2 registers on every connection."""
    if a is None or b is None:
        return None
    return (int.from_bytes(phash_bytes(a), "big") ^ int.from_bytes(phash_bytes(b), "big")).bit_count()


def decision_int(ref: str | None) -> int | None:
    """`dec-000812` -> 812 (4.14 ids; 8.3 stores the integer)."""
    if ref is None:
        return None
    m = _DECISION_RE.match(ref)
    if not m:
        raise StoreRefused(f"decision id {ref!r} is not dec-<sequence> (4.14)")
    return int(m.group(1))


def decision_ref(n: int | None) -> str | None:
    return None if n is None else f"dec-{n:06d}"


def _sql_list(values: Iterable[str]) -> str:
    return ",".join("'" + v.replace("'", "''") + "'" for v in values)


def _pattern(p: str) -> tuple[str, int, str | None, str]:
    """A taxonomy reason pattern -> (code, parametric, detail placeholder, separator)."""
    if "<" not in p:
        return p, 0, None, ":"
    i = p.index("<")
    return p[: i - 1], 1, p[i:], p[i - 1]


def _vocab(t: Taxonomy, name: str) -> list[str]:
    if name == "reason_codes":
        return sorted({_pattern(p)[0] for pats in t.data["reasons"].values() for p in pats})
    try:
        return list(t.list(name))
    except KeyError:
        raise StoreError(f"schema.sql names taxonomy list {name!r}, which taxonomy.yaml lacks") from None


def _not_run_reason_check(t: Taxonomy) -> str:
    pats = [_pattern(p) for p in t.data["reasons"]["not_run"]]
    expr = f"not_run_reason IN ({_sql_list(c for c, par, _, _ in pats if not par)})"
    return expr + "".join(f" OR not_run_reason LIKE '{c}{sep}%'" for c, par, _, sep in pats if par)


def _action_verdict_check(t: Taxonomy) -> str:
    actions, verdicts = t.list("decision_actions"), t.list("decision_verdicts")
    mapping = {a: (v,) for a, v in ACTION_VERDICT.items()} | MULTI_VERDICT
    unmapped = [a for a in actions if a not in mapping]
    unknown = [x for a, vs in mapping.items() for x in (a, *vs) if x not in actions and x not in verdicts]
    if unmapped or unknown:
        raise StoreError(f"decision mapping and taxonomy.yaml disagree: unmapped {unmapped}, unknown {unknown}")
    clauses = []
    for v in verdicts:
        group = [a for a in actions if mapping[a] == (v,)]
        if group:
            clauses.append(f"(action IN ({_sql_list(group)}) AND verdict = {_sql_list([v])})")
    for a in actions:
        if len(mapping[a]) > 1:
            clauses.append(f"(action = {_sql_list([a])} AND verdict IN ({_sql_list(mapping[a])}))")
    return "\n      OR ".join(clauses)


def render_schema(t: Taxonomy) -> str:
    """schema.sql with every placeholder filled from the taxonomy (8.3, L-OUT-01)."""
    template = resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")

    def sub(m: re.Match) -> str:
        name, items = m.group(1), m.group(2)
        if name == "not_run_reason":
            return _not_run_reason_check(t)
        if name == "decision_action_verdict":
            return _action_verdict_check(t)
        values = _vocab(t, name)
        if items is None:
            return _sql_list(values)
        wanted = items.split(",")
        missing = [w for w in wanted if w not in values]
        if missing:
            raise StoreError(f"schema.sql names {missing}, which taxonomy.yaml {name} lacks")
        return _sql_list(wanted)

    return _PLACEHOLDER.sub(sub, template)


def seed_rows(t: Taxonomy) -> list[tuple[str, str, int, str | None, str]]:
    """The outcome_reasons seed (outcome, reason, parametric, detail_means, owner_section) from taxonomy.yaml."""
    rows: dict[tuple[str, str], tuple[str, str, int, str | None, str]] = {}
    for outcome, pats in t.data["reasons"].items():
        for p in pats:
            code, par, means, _ = _pattern(p)
            rows.setdefault((outcome, code), (outcome, code, par, means, "4.13"))
    for child, parent in t.data.get("reason_inheritance", {}).items():
        for (o, code), (_, _, par, means, _) in list(rows.items()):
            if o == parent:
                rows.setdefault((child, code), (child, code, par, means, "1.10"))
    return list(rows.values())


def _seed_sql(rows: Sequence[tuple]) -> str:
    def lit(v: Any) -> str:
        return "NULL" if v is None else str(v) if isinstance(v, int) else _sql_list([v])
    values = ",\n ".join("(" + ",".join(lit(v) for v in r) + ")" for r in rows)
    return ("INSERT INTO outcome_reasons (outcome, reason, parametric, detail_means, owner_section) VALUES\n "
            + values + ";\n")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5                # ERROR_ACCESS_DENIED: it exists
        try:
            code = wintypes.DWORD()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_local_volume(path: str | os.PathLike) -> None:
    """Refuse a database or artifact root on a share (8.2, preflight H7, 8.9)."""
    s = os.fspath(path)
    if s.startswith(("\\\\", "//")):
        raise StoreError(f"{s} is a UNC path; the store needs a fixed local volume (8.2, H7)")
    if sys.platform == "win32":
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(s))[0] + "\\"
        if ctypes.windll.kernel32.GetDriveTypeW(drive) != 3:   # DRIVE_FIXED
            raise StoreError(f"{s} is not on a fixed local drive (8.2, H7)")
        return
    mounts = Path("/proc/mounts")
    if not mounts.exists():
        return
    target = os.path.realpath(s)
    best, fstype = "", ""
    for line in mounts.read_text().splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = parts[1].replace("\\040", " ")
        if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
            best, fstype = mnt, parts[2]
    if fstype in NETWORK_FS:
        raise StoreError(f"{s} is on a network file system ({fstype}); the store needs a local volume (8.2, H7)")


class WriterLock:
    """`writer.lock` beside the database, holding the writer's PID (8.2, 8.4)."""

    STALE_EMPTY_SEC = 60

    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def acquire(self) -> None:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if not self._stale():
                    raise WriterLockError(f"another writer holds {self.path} ({self._text()!r}); 8.2 allows one")
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.path)
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
                f.flush()
                os.fsync(f.fileno())
            self.held = True
            return
        raise WriterLockError(f"could not take {self.path}")

    def _text(self) -> str:
        try:
            return self.path.read_text().strip()
        except OSError:
            return ""

    def _stale(self) -> bool:
        text = self._text()
        if text.isdigit():
            return not _pid_alive(int(text))
        try:                                       # a writer between create and write leaves it empty briefly
            return time.time() - self.path.stat().st_mtime > self.STALE_EMPTY_SEC
        except FileNotFoundError:
            return True

    def release(self) -> None:
        if self.held and self._text() == str(os.getpid()):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)
        self.held = False


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.create_function("hamming", 2, hamming, deterministic=True)


def connect_reader(path: str | os.PathLike, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """A read-only connection: `mode=ro`, `PRAGMA query_only = 1`, `hamming()` registered (8.2, 8.7)."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
    _configure(conn)
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA query_only = 1")
    return conn


# ---------------------------------------------------------------------------------------------------------------
# the decision lookup (8.3) and inheritance (4.14), usable on any connection with the decisions table

_LOOKUP_WHERE = """
WHERE checkpoint_id = :cp AND region_id = :region AND category = :cat
  AND verdict IS NOT NULL AND status IN ('active','reopened')
  AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > :now)"""
LOOKUP_SQL = ("SELECT *, hamming(phash, :phash) AS distance FROM decisions" + _LOOKUP_WHERE + """
  AND hamming(phash, :phash) <= hamming_tolerance
ORDER BY hamming(phash, :phash), decided_at DESC""")   # 8.3: no LIMIT, the verdict set is the answer (9.7)
RELATED_SQL = ("SELECT *, hamming(phash, :phash) AS distance FROM decisions" + _LOOKUP_WHERE + """
  AND hamming(phash, :phash) > hamming_tolerance AND hamming(phash, :phash) <= :related
ORDER BY hamming(phash, :phash), decided_at DESC""")


def _decision_from_row(r: sqlite3.Row) -> Decision:
    return Decision(
        decision_id=decision_ref(r["decision_id"]), action=r["action"], verdict=r["verdict"],
        signature=Signature(r["checkpoint_id"], r["region_id"], bytes(r["phash"]).hex(), r["category"]),
        decided_by=r["decided_by"], hamming_tolerance=r["hamming_tolerance"], ticket=r["ticket_ref"],
        status=r["status"], source=r["source"], reason=r["reason"], decided_at=r["decided_at"])


def lookup_decisions(conn: sqlite3.Connection, signature: Signature, *, at: str | None = None,
                     rescued: bool = False, synthetic: bool = False) -> DecisionLookup:
    """Every active decision within tolerance of `signature`, plus the related ones up to 12 bits (8.3, 4.14).

    A rescued checkpoint (5.5) and a synthetic outcome (10.3) never run the query: the answer is empty.
    """
    if rescued or synthetic:
        return DecisionLookup()
    args = {"cp": signature.checkpoint_id, "region": signature.region_id, "cat": signature.category,
            "phash": phash_bytes(signature.phash), "now": utc(at) or now(), "related": RELATED_LIMIT}
    found = [DecisionMatch(_decision_from_row(r), r["distance"]) for r in conn.execute(LOOKUP_SQL, args)]
    related = [DecisionMatch(_decision_from_row(r), r["distance"]) for r in conn.execute(RELATED_SQL, args)]
    return DecisionLookup(tuple(found), tuple(related))


def apply_inheritance(outcome: CheckpointOutcome, lookup: DecisionLookup) -> CheckpointOutcome:
    """The 4.14 inheritance rule for one computed outcome; returns a new record, never edits `outcome`.

    No match, a conflict (F25), a rescued or a synthetic row: unchanged. `invariant_violation` and
    `assertion_failed` keep their outcome and only carry the decision (it suppresses the finding). A `noise` or
    `intended` verdict sets that outcome with `decision:<id>`; a `confirmed_bug` verdict keeps the computed
    outcome and links the decision (a computed `collateral` carries reason `decision:<id>`, 8.3 seed).
    """
    d = lookup.inherited
    if d is None or outcome.rescued or outcome.synthetic:
        return outcome
    if outcome.outcome in ("invariant_violation", "assertion_failed"):
        return dataclasses.replace(outcome, decision_ref=d.decision_id)
    if d.verdict in ("noise", "intended"):
        return dataclasses.replace(outcome, outcome=d.verdict, reason=f"decision:{d.decision_id}",
                                   decision_ref=d.decision_id, review_state="none")
    if outcome.outcome == "collateral" and outcome.reason is None:
        return dataclasses.replace(outcome, reason=f"decision:{d.decision_id}", decision_ref=d.decision_id)
    return dataclasses.replace(outcome, decision_ref=d.decision_id)


# ---------------------------------------------------------------------------------------------------------------
# record <-> row

def _outcome_from_dict(d: dict) -> CheckpointOutcome:
    d = dict(d)
    if "diff" in d:
        diff = dict(d["diff"])
        diff["shift"] = tuple(diff.get("shift", (0, 0)))
        diff["regions"] = [RegionDiff(**r) for r in diff.get("regions", [])]
        diff["components"] = [Component(**{**c, "bbox": tuple(c["bbox"])}) for c in diff.get("components", [])]
        d["diff"] = DiffSummary(**diff)
    if "classification" in d:
        d["classification"] = Classification(**d["classification"])
    return CheckpointOutcome(**d)


def _revalidate(record):
    """Records are mutable dataclasses: rebuilding one re-runs its taxonomy checks (TaxonomyError)."""
    return dataclasses.replace(record)


def _json(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), sort_keys=True, separators=(",", ":"))


def _ledger_default(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    raise TypeError(type(v).__name__)


# ---------------------------------------------------------------------------------------------------------------

class Store:
    """The single writer of one Sentinel database (8.2). Use as a context manager or call `close()`."""

    def __init__(self, path: str | os.PathLike, *, taxonomy: Taxonomy | None = None, ledger: bool = True,
                 busy_timeout_ms: int = 5000, require_local: bool = True):
        self.path = Path(path)
        self.taxonomy = taxonomy or load()
        if require_local:
            check_local_volume(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = WriterLock(self.path.parent / "writer.lock")
        self._lock.acquire()
        try:
            self._conn = sqlite3.connect(str(self.path), isolation_level=None)
            _configure(self._conn)
            for p in ("journal_mode = WAL", "synchronous = NORMAL", "foreign_keys = ON",
                      f"busy_timeout = {int(busy_timeout_ms)}", "wal_autocheckpoint = 1000"):
                self._conn.execute(f"PRAGMA {p}")
            self._ledger = self.path.parent / "results.jsonl" if ledger else None
            self._forms = {(o, c): (par, sep) for o, pats in self.taxonomy.data["reasons"].items()
                           for c, par, _, sep in map(_pattern, pats)}
            for child, parent in self.taxonomy.data.get("reason_inheritance", {}).items():
                for (o, c), form in list(self._forms.items()):
                    if o == parent:
                        self._forms.setdefault((child, c), form)
            self._open_schema()
        except BaseException:
            if hasattr(self, "_conn"):
                self._conn.close()
            self._lock.release()
            raise

    # -- lifecycle -----------------------------------------------------------------------------------------
    def close(self) -> None:
        if getattr(self, "_conn", None) is not None:
            self._conn.close()
            self._conn = None
        self._lock.release()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        """The writer connection, for reads in this process. Write only through the methods."""
        return self._conn

    def _open_schema(self) -> None:
        ddl = render_schema(self.taxonomy)
        digest = hashlib.sha256(ddl.encode()).hexdigest()
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            if self._conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
                raise StoreError(f"{self.path} holds tables but no Sentinel schema version")
            at = now()
            state = _sql_list(["schema_version", str(SCHEMA_VERSION), at]), _sql_list(["schema_sha256", digest, at])
            self._conn.executescript(
                "BEGIN IMMEDIATE;\n" + ddl + "\n" + _seed_sql(seed_rows(self.taxonomy))
                + "".join(f"INSERT INTO system_state (key, value, updated_at) VALUES ({s});\n" for s in state)
                + f"PRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;")
            return
        if version != SCHEMA_VERSION:
            raise StoreError(f"{self.path} has schema version {version}; this store reads {SCHEMA_VERSION} and has "
                             "no migration for it (migrations are forward-only, after VACUUM INTO, 8.6)")
        row = self._conn.execute("SELECT value FROM system_state WHERE key = 'schema_sha256'").fetchone()
        if row is None or row[0] != digest:
            raise StoreError(f"{self.path} was created from a different taxonomy.yaml (a generated CHECK changed); "
                             "it needs a migration before this store can write it (8.3, 8.6)")
        self._sync_reasons()

    def _sync_reasons(self) -> None:
        """8.3: the reason table is seeded from taxonomy.yaml at service start; reasons are retired, never deleted."""
        rows = seed_rows(self.taxonomy)
        keys = {(r[0], r[1]) for r in rows}
        with self._batch("reasons_sync") as _:
            self._conn.executemany("INSERT OR IGNORE INTO outcome_reasons (outcome, reason, parametric, "
                                   "detail_means, owner_section) VALUES (?,?,?,?,?)", rows)
            for o, r, active in self._conn.execute("SELECT outcome, reason, active FROM outcome_reasons").fetchall():
                want = 1 if (o, r) in keys else 0
                if active != want:
                    self._conn.execute("UPDATE outcome_reasons SET active = ? WHERE outcome = ? AND reason = ?",
                                       (want, o, r))

    # -- transactions and the ledger -----------------------------------------------------------------------
    @contextlib.contextmanager
    def _batch(self, kind: str) -> Iterator[list[dict]]:
        """One BEGIN IMMEDIATE transaction; its rows go to results.jsonl before COMMIT (8.2)."""
        if self._conn is None:
            raise StoreError("store is closed")
        for attempt in range(4):                       # 8.9: busy timeout and 3 retries
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                if attempt == 3 or not any(w in str(e) for w in ("locked", "busy")):
                    raise StoreWriteFailed(f"cannot begin a write: {e}") from e
        rows: list[dict] = []
        try:
            yield rows
            if self._ledger is not None and rows:
                line = json.dumps({"batch": kind, "at": now(), "rows": rows}, sort_keys=True,
                                  default=_ledger_default)
                with open(self._ledger, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            self._conn.execute("COMMIT")
        except BaseException as e:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            if isinstance(e, sqlite3.IntegrityError):
                raise StoreRefused(f"refused by the schema: {e}") from e
            if isinstance(e, sqlite3.OperationalError):
                raise StoreWriteFailed(f"write failed: {e}") from e
            if isinstance(e, OSError):
                raise StoreWriteFailed(f"ledger append failed: {e}") from e
            raise

    def _insert(self, rows: list[dict], table: str, values: dict, *, upsert: str | None = None) -> int:
        cols = list(values)
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        if upsert:
            sql += f" ON CONFLICT {upsert}"
        cur = self._conn.execute(sql, [values[c] for c in cols])
        rows.append({"op": "upsert" if upsert else "insert", "table": table, "row": values})
        return cur.lastrowid

    def _update(self, rows: list[dict], table: str, key: dict, values: dict) -> None:
        sets = ",".join(f"{c} = ?" for c in values)
        where = " AND ".join(f"{c} = ?" for c in key)
        self._conn.execute(f"UPDATE {table} SET {sets} WHERE {where}", [*values.values(), *key.values()])
        rows.append({"op": "update", "table": table, "key": key, "set": values})

    def _one(self, sql: str, args: Sequence = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, args).fetchone()

    # -- reasons -------------------------------------------------------------------------------------------
    def _split_reason(self, outcome: str, reason: str | None) -> tuple[str | None, str | None]:
        if reason is None or reason == "":
            return None, None
        code, sep, param = reason.partition(":")
        if sep:
            return code, param
        head, dot, tail = reason.partition(".")
        if dot and self._forms.get((outcome, head), (0, ":"))[1] == ".":
            return head, tail
        return reason, None

    def _join_reason(self, outcome: str, code: str | None, param: str | None) -> str | None:
        if code is None:
            return None
        parametric, sep = self._forms.get((outcome, code), (0, ":"))
        return f"{code}{sep}{param}" if parametric else code

    def _encode_reason(self, o: CheckpointOutcome) -> tuple[str | None, str | None]:
        """(reason code, reason_detail) for an outcome record; see the module docstring."""
        code, param = self._split_reason(o.outcome, o.reason)
        if param is not None and "\n" in param:
            raise StoreRefused("a reason parameter is one line (8.3)")
        detail = o.detail
        if detail is None and o.outcome == "invariant_violation" and o.invariant_ids and param is None:
            detail = ",".join(o.invariant_ids)
        if param is None:
            return code, detail
        return code, param if detail is None else f"{param}\n{detail}"

    def _decode_reason(self, outcome: str, code: str | None, stored: str | None) -> tuple[str | None, str | None]:
        if code is None:
            return None, stored
        if self._forms.get((outcome, code), (0, ":"))[0]:
            param, _, detail = (stored or "").partition("\n")
            return self._join_reason(outcome, code, param), detail or None
        return code, stored

    # -- identity: logins, rigs, builds, checkpoints -------------------------------------------------------
    def register_rig(self, rig_id: str, *, login_alias: str, owner: str, snapshot_id: str, allowlist_entry: int,
                     tier: str = "A", role: str = "pool", login_kind: str = "scripted") -> None:
        """Add or refresh a rig and its login (8.3 `rigs`, `logins`); the vault alias only, never a credential."""
        with self._batch("rig") as rows:
            if self._one("SELECT 1 FROM logins WHERE login_alias = ?", (login_alias,)) is None:
                self._insert(rows, "logins", {"login_alias": login_alias, "kind": login_kind,
                                              "allowlist_entry": allowlist_entry, "owner": owner})
            self._insert(rows, "rigs", {"rig_id": rig_id, "tier": tier, "role": role, "login_alias": login_alias,
                                        "snapshot_id": snapshot_id, "active": 1},
                         upsert="(rig_id) DO UPDATE SET tier = excluded.tier, role = excluded.role, "
                                "login_alias = excluded.login_alias, snapshot_id = excluded.snapshot_id, active = 1")

    def register_build(self, build_id: str, *, channel: str, source_commit: str, pipeline_artifact_url: str,
                       installer_sha256: str, branch: str | None = None, first_seen_at: str | None = None,
                       released_at: str | None = None) -> None:
        """Add a build (8.3 `builds`, 4.2). A build id read again with different facts is an import conflict."""
        row = {"build_id": build_id, "channel": channel, "source_commit": source_commit, "branch": branch,
               "pipeline_artifact_url": pipeline_artifact_url, "installer_sha256": installer_sha256,
               "first_seen_at": utc(first_seen_at) or now(), "released_at": utc(released_at)}
        old = self._one("SELECT * FROM builds WHERE build_id = ?", (build_id,))
        if old is not None:
            same = all(old[k] == row[k] for k in ("channel", "source_commit", "installer_sha256"))
            if not same:
                raise ImportConflict(f"build {build_id} is already recorded with other facts (4.2)")
            return
        with self._batch("build") as rows:
            self._insert(rows, "builds", row)

    def _checkpoint_row(self, e: ScopeEntry, config_commit: str) -> dict:
        parts = checkpoint_parts(e.checkpoint_id)
        kind, q = parts["kind"], parts["qualifier"].split("/")
        if e.persona not in PERSONAS:
            raise TaxonomyError(f"persona {e.persona!r}")
        if e.data_mode not in DATA_MODES:
            raise TaxonomyError(f"data_mode {e.data_mode!r}")
        row: dict[str, Any] = {"checkpoint_id": e.checkpoint_id, "kind": kind, "surface_id": parts["surface"],
                               "workspace_id": e.workspace_id, "tier": e.tier, "data_mode": e.data_mode,
                               "recapturable": int(e.recapturable), "first_seen_commit": config_commit}
        if kind == "census" and q[0] != e.workspace_id:
            raise StoreRefused(f"{e.checkpoint_id}: a census id's qualifier is its workspace id (8.3)")
        if kind == "sweep":
            w, h = q[1].split("x")
            row.update(host=q[0], size_w=int(w), size_h=int(h))
        elif kind == "split":
            row.update(splitter_id=q[0], fraction=float(q[1]))
        elif kind == "flow":
            if not e.flow_class:
                raise StoreRefused(f"{e.checkpoint_id}: a flow checkpoint needs its flow_class (8.3)")
            row.update(flow_id=q[0], step_id=q[1], flow_class=e.flow_class)
        return row

    def _upsert_checkpoint(self, rows: list[dict], e: ScopeEntry, config_commit: str) -> None:
        self._insert(rows, "checkpoints", self._checkpoint_row(e, config_commit),
                     upsert="(checkpoint_id) DO UPDATE SET workspace_id = excluded.workspace_id, tier = excluded.tier, "
                            "data_mode = excluded.data_mode, flow_class = excluded.flow_class, "
                            "recapturable = excluded.recapturable")

    def register_checkpoints(self, entries: Iterable[ScopeEntry], *, config_commit: str) -> None:
        """Add or refresh `checkpoints` rows (8.3) outside a run's scope, e.g. for probe-found surfaces."""
        with self._batch("checkpoints") as rows:
            for e in entries:
                self._upsert_checkpoint(rows, e, config_commit)

    # -- runs ----------------------------------------------------------------------------------------------
    def begin_run(self, plan: RunPlan) -> None:
        """Write the run, its checkpoints, its scope and its shards in one batch; lifecycle `running` (8.3)."""
        if plan.run_type not in RUN_TYPES:
            raise TaxonomyError(f"run_type {plan.run_type!r}")
        ids = [e.checkpoint_id for e in plan.scope]
        if len(ids) != len(set(ids)):
            raise StoreRefused("a checkpoint appears twice in the scope (one row per checkpoint per run, 8.3)")
        if self._one("SELECT 1 FROM runs WHERE run_id = ?", (plan.run_id,)) is not None:
            raise StoreRefused(f"run {plan.run_id} has already begun")
        with self._batch("run_begin") as rows:
            self._insert(rows, "runs", {
                "run_id": plan.run_id, "run_type": plan.run_type, "pair_id": plan.pair_id, "lifecycle": "running",
                "config_commit": plan.config_commit, "config_tarball_sha256": plan.config_tarball_sha256,
                "snapshot_set_sha256": plan.snapshot_set_sha256, "harness_sha": plan.harness_sha,
                "authorization_ref": plan.authorization_ref, "manifest_sha256": plan.manifest_sha256,
                "pr_ref": plan.pr_ref, "pr_author": plan.pr_author, "budget_sec": plan.budget_sec,
                "started_at": utc(plan.started_at), "scope_count": len(ids)})
            for e in plan.scope:
                self._upsert_checkpoint(rows, e, plan.config_commit)
                self._insert(rows, "run_scope", {"run_id": plan.run_id, "checkpoint_id": e.checkpoint_id,
                                                 "persona": e.persona})
            for s in plan.shards:
                if not s.shard_id.startswith(plan.run_id + "/"):
                    raise StoreRefused(f"shard id {s.shard_id!r} is not <run_id>/<n> (8.3)")
                self._insert(rows, "shards", {
                    "shard_id": s.shard_id, "run_id": plan.run_id, "rig_id": s.rig_id, "attempt": s.attempt,
                    "sides": s.sides, "scope_count": len(ids) if s.scope_count is None else s.scope_count,
                    "state": "running", "started_at": utc(plan.started_at)})

    def _running(self, run_id: str) -> sqlite3.Row:
        run = self._one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if run is None:
            raise StoreRefused(f"unknown run {run_id}")
        if run["lifecycle"] == "finished":
            raise StoreRefused(f"run {run_id} is finished; its outcome rows are final (4.13)")
        return run

    def end_shard(self, shard_id: str, status: str, reason: str | None = None) -> None:
        """The shard's own outcome at teardown (8.3 `shards.shard_outcome`), state `finished`."""
        self.taxonomy.check_run(status, reason)
        code, param = self._split_reason(status, reason)
        with self._batch("shard_end") as rows:
            self._update(rows, "shards", {"shard_id": shard_id},
                         {"state": "finished", "shard_outcome": status, "abort_reason": code,
                          "abort_detail": param, "finished_at": now()})

    def mark_shard_imported(self, shard_id: str, *, run_store_sha256: str | None = None,
                            ledger_sha256: str | None = None, ledger_rows: int | None = None) -> None:
        """Mark a shard imported (8.2 shard import, done by the import), then checkpoint the WAL passively."""
        with self._batch("shard_import") as rows:
            self._update(rows, "shards", {"shard_id": shard_id},
                         {"state": "imported", "imported_at": now(), "run_store_sha256": run_store_sha256,
                          "ledger_sha256": ledger_sha256, "ledger_rows": ledger_rows})
        self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def run_counts(self, run_id: str, *, include_synthetic: bool = False) -> dict[str, int]:
        """Outcome counts of a run over every checkpoint outcome of the taxonomy, zeros included (4.13)."""
        counts = dict.fromkeys(self.taxonomy.checkpoint_outcomes, 0)
        sql = "SELECT outcome, count(*) FROM outcomes WHERE run_id = ?" + (
            "" if include_synthetic else " AND synthetic = 0") + " GROUP BY outcome"
        for outcome, n in self._conn.execute(sql, (run_id,)):
            counts[outcome] = n
        return counts

    def missing_outcomes(self, run_id: str) -> list[str]:
        """Scoped checkpoint ids of the run with no non-synthetic outcome row, sorted."""
        return [r[0] for r in self._conn.execute(
            "SELECT s.checkpoint_id FROM run_scope s WHERE s.run_id = ? AND NOT EXISTS (SELECT 1 FROM outcomes o "
            "WHERE o.run_id = s.run_id AND o.checkpoint_id = s.checkpoint_id AND o.synthetic = 0) "
            "ORDER BY s.checkpoint_id", (run_id,))]

    def complete_blockers(self, run_id: str) -> list[str]:
        """Why the run cannot be `complete` now; empty when it can (8.1, 8.3 run trigger, 8.9)."""
        out = []
        missing = self.missing_outcomes(run_id)
        if missing:
            out.append(f"{len(missing)} scoped checkpoint(s) without an outcome, first {missing[0]}")

        def q(sql: str) -> int:
            return self._one(sql, (run_id,))[0]

        if n := q("SELECT count(*) FROM outcomes WHERE run_id = ? AND synthetic = 0 AND outcome = 'env_error'"):
            out.append(f"{n} env_error outcome(s)")
        if n := q("SELECT count(*) FROM outcomes WHERE run_id = ? AND outcome = 'harness_error' AND reason IN ("
                  + _sql_list(STORE_FAILURE_REASONS) + ")"):
            out.append(f"{n} store-failure harness_error outcome(s)")
        if n := q("SELECT count(*) FROM shards WHERE run_id = ? AND state <> 'imported'"):
            out.append(f"{n} shard(s) not imported")
        if n := q("SELECT count(*) FROM outcomes o WHERE o.run_id = ? AND ("
                  + " OR ".join(f"(o.{c} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM artifacts a WHERE a.sha256 = o.{c}))"
                                for c in ("cand_capture_sha256", "base_capture_sha256", "heatmap_sha256")) + ")"):
            out.append(f"{n} outcome(s) naming an artifact the store lacks")
        return out

    def end_run(self, outcome: RunOutcome) -> RunOutcome:
        """Write the run outcome (8.2: the worst of the shards, decided by the caller); counts are computed here.

        `complete` is refused with `RunNotComplete` while `complete_blockers` lists anything.
        """
        r = _revalidate(outcome)
        run = self._one("SELECT * FROM runs WHERE run_id = ?", (r.run_id,))
        if run is None:
            raise StoreRefused(f"unknown run {r.run_id}")
        code, param = self._split_reason(r.status, r.reason)
        if run["lifecycle"] == "finished":
            if (run["outcome"], run["abort_reason"], run["abort_detail"]) == (r.status, code, param):
                return self.run_outcome(r.run_id)
            raise ImportConflict(f"run {r.run_id} already ended {run['outcome']}")
        if code is not None and self._forms.get((r.status, code), (0, ":"))[0] and not param:
            raise StoreRefused(f"reason {code} needs its parameter (8.3)")
        if r.status == "complete" and (blockers := self.complete_blockers(r.run_id)):
            raise RunNotComplete(f"run {r.run_id} cannot be complete: " + "; ".join(blockers))
        counts = self.run_counts(r.run_id)
        ended = utc(r.ended_at) or now()
        with self._batch("run_end") as rows:
            self._update(rows, "runs", {"run_id": r.run_id}, {
                "lifecycle": "finished", "outcome": r.status, "abort_reason": code, "abort_detail": param,
                "finished_at": ended, "summary_json": json.dumps({"counts": counts, "last_gate": r.last_gate},
                                                                 sort_keys=True)})
        return self.run_outcome(r.run_id)

    def run_outcome(self, run_id: str) -> RunOutcome | None:
        """The finished run as a records.RunOutcome; None while the run is not finished."""
        run = self._one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if run is None or run["outcome"] is None:
            return None
        summary = json.loads(run["summary_json"] or "{}")
        return RunOutcome(run_id=run_id, run_type=run["run_type"], status=run["outcome"],
                          last_gate=summary.get("last_gate", "reported"),
                          reason=self._join_reason(run["outcome"], run["abort_reason"], run["abort_detail"]),
                          counts=summary.get("counts", self.run_counts(run_id)),
                          started_at=run["started_at"], ended_at=run["finished_at"])

    # -- outcomes, steps, findings, artifacts ----------------------------------------------------------------
    def _outcome_row(self, o: CheckpointOutcome, shard_id: str | None) -> dict:
        code, detail = self._encode_reason(o)
        did = decision_int(o.decision_ref)
        if code == "decision":
            from_reason = decision_int(self._split_reason(o.outcome, o.reason)[1])
            if did is not None and did != from_reason:
                raise StoreRefused("reason decision:<id> and decision_ref name different decisions (4.13)")
            did = from_reason
        arts = o.artifacts or {}
        diff = o.diff
        largest = max(diff.components, key=lambda c: c.area) if diff and diff.components else None
        return {
            "run_id": o.run_id, "checkpoint_id": o.checkpoint_id, "shard_id": shard_id, "side": o.side,
            "gate": o.gate, "outcome": o.outcome, "reason": code, "reason_detail": detail,
            "rescued": int(o.rescued), "review_state": o.review_state,
            "cand_capture_sha256": arts.get("candidate"), "base_capture_sha256": arts.get("base"),
            "heatmap_sha256": arts.get("heatmap"),
            "diff_pct": diff.whole_pixel_fraction * 100 if diff else None, "ssim": diff.whole_ssim if diff else None,
            "shift_dx": diff.shift[0] if diff else None, "shift_dy": diff.shift[1] if diff else None,
            "components": len(diff.components) if diff else None,
            "largest_bbox": ",".join(map(str, largest.bbox)) if largest else None,
            "threshold_source": o.threshold_source, "recaptures": o.recaptures, "stability": o.stability,
            "stability_cause": o.stability_cause, "decision_id": did, "manifest_entry": o.manifest_entry_ref,
            "primitive": o.primitive, "synthetic": int(o.synthetic), "recorded_at": utc(o.recorded_at)}

    def _step_row(self, s: StepOutcome, shard_id: str, side: str, checkpoint_id: str | None) -> dict:
        seq = self._one("SELECT COALESCE(MAX(seq), 0) + 1 FROM step_events WHERE shard_id = ?", (shard_id,))[0]
        agent = s.status == "passed_via_agent_fallback" or s.locator_rung == "agent"
        return {"run_id": s.run_id, "shard_id": shard_id, "side": side, "seq": seq, "source": "orchestrator",
                "flow_id": s.flow_id, "step_id": s.step_id, "checkpoint_id": checkpoint_id,
                "wall_at": utc(s.recorded_at), "mono_ms": time.monotonic_ns() // 1_000_000,
                "mode": "agent" if agent else "scripted", "action": "step",
                "locator_level": s.locator_rung if s.locator_rung in LOCATOR_LEVELS else None,
                "attempt": s.attempt, "step_outcome": s.status, "gate_decision": "none",
                "reason": f"finding:{s.finding_ref}" if s.finding_ref else None, "duration_ms": s.duration_ms}

    def _finding_row(self, f: Finding, synthetic: bool) -> dict:
        return {"natural_key": f.finding_id, "run_id": f.run_id, "checkpoint_id": f.checkpoint_id, "kind": f.kind,
                "tier": f.tier, "region_id": f.region_id, "primitive": f.primitive,
                "manifest_entry": f.manifest_entry_ref, "phash": phash_bytes(f.phash) if f.phash else None,
                "category": f.category, "description": f.description,
                "evidence_json": json.dumps(f.evidence, sort_keys=True), "decision_id": decision_int(f.decision_ref),
                "ticket_ref": f.ticket, "triage_state": f.triage_state, "synthetic": int(synthetic),
                "first_seen_run_id": f.first_seen_run_id, "created_at": utc(f.created_at)}

    def _put_finding(self, rows: list[dict], f: Finding, synthetic: bool) -> None:
        row = self._finding_row(f, synthetic)
        old = self._one("SELECT * FROM findings WHERE natural_key = ?", (f.finding_id,))
        if old is None:
            self._insert(rows, "findings", row)
            return
        later = {"decision_id", "ticket_ref", "triage_state"}          # 4.14: decisions move these
        if any(old[k] != v for k, v in row.items() if k not in later):
            raise ImportConflict(f"finding {f.finding_id} is already recorded with other content (8.2)")

    def _put_artifact(self, rows: list[dict], a: StoredArtifact, run_id: str) -> None:
        if self._one("SELECT 1 FROM artifacts WHERE sha256 = ?", (a.sha256,)) is not None:
            return                                   # 8.4: a hash already indexed; the first set keeps the row
        self._insert(rows, "artifacts", {
            "sha256": a.sha256, "kind": a.kind, "media_type": a.media_type, "bytes": a.bytes, "width": a.width,
            "height": a.height, "redaction_passed": 1, "redaction_version": a.redaction_version,
            "set_path": a.set_path, "first_run_id": run_id, "created_at": now()})

    def _ensure_checkpoint(self, rows: list[dict], o: CheckpointOutcome) -> None:
        if self._one("SELECT 1 FROM checkpoints WHERE checkpoint_id = ?", (o.checkpoint_id,)) is not None:
            return
        if o.tier is None or o.test_kind == "flow":
            raise StoreRefused(f"{o.checkpoint_id} is not registered; register it (tier, flow_class) first (8.3)")
        commit = self._one("SELECT config_commit FROM runs WHERE run_id = ?", (o.run_id,))[0]
        self._upsert_checkpoint(rows, ScopeEntry(o.checkpoint_id, o.workspace_id, o.persona, o.tier, o.data_mode),
                                commit)

    def write_checkpoint(self, outcome: CheckpointOutcome, *, steps: Sequence[StepOutcome] = (),
                         findings: Sequence[Finding] = (), artifacts: Sequence[StoredArtifact] = (),
                         shard_id: str | None = None, step_side: str = "candidate") -> None:
        """Write one checkpoint's batch (8.2): its artifact rows, its outcome, its step events and its findings.

        Refuses: anything outside the taxonomy (TaxonomyError); a finished or unknown run; `review_state:
        pending` without reason reviewer_unavailable or global_shift (4.13); an artifact the batch and the store
        lack (8.2); a different record for an outcome already written (ImportConflict); any 8.3 constraint.
        Writing the same record again is a no-op (replay is idempotent, 8.2).
        """
        o = _revalidate(outcome)
        steps = [_revalidate(s) for s in steps]
        findings = [_revalidate(f) for f in findings]
        self._running(o.run_id)
        record = _json(o)
        old = self._one("SELECT record_json FROM outcome_records WHERE run_id = ? AND checkpoint_id = ?",
                        (o.run_id, o.checkpoint_id))
        if old is not None:
            if old[0] == record:
                return
            raise ImportConflict(f"{o.checkpoint_id} in {o.run_id} already has an outcome; outcomes are written "
                                 "once (4.13, 8.2)")
        code = self._split_reason(o.outcome, o.reason)[0]
        if o.review_state == "pending" and code not in PENDING_REASONS:
            raise StoreRefused("review_state pending only with reason reviewer_unavailable or global_shift (4.13)")
        named = [h for h in (o.artifacts or {}).get("crops", [])] + [
            (o.artifacts or {}).get(k) for k in ("candidate", "base", "heatmap")]
        batch_hashes = {a.sha256 for a in artifacts}
        for h in filter(None, named):
            if h not in batch_hashes and self._one("SELECT 1 FROM artifacts WHERE sha256 = ?", (h,)) is None:
                raise StoreRefused(f"{o.checkpoint_id} names artifact {h}, which was not stored (8.2)")
        if steps and shard_id is None:
            raise StoreRefused("step events need the shard they ran in (8.3 step_events.shard_id)")
        with self._batch("checkpoint") as rows:
            self._ensure_checkpoint(rows, o)
            for a in artifacts:
                self._put_artifact(rows, a, o.run_id)
            self._insert(rows, "outcomes", self._outcome_row(o, shard_id))
            self._insert(rows, "outcome_records", {"run_id": o.run_id, "checkpoint_id": o.checkpoint_id,
                                                   "record_json": record})
            for s in steps:
                self._insert(rows, "step_events", self._step_row(s, shard_id, step_side, o.checkpoint_id))
            for f in findings:
                self._put_finding(rows, f, o.synthetic)

    def write_steps(self, steps: Sequence[StepOutcome], *, shard_id: str, side: str,
                    checkpoint_id: str | None = None) -> None:
        """Write step outcomes as `step_events` rows outside a checkpoint batch (8.3)."""
        steps = [_revalidate(s) for s in steps]
        for s in steps:
            self._running(s.run_id)
        with self._batch("steps") as rows:
            for s in steps:
                self._insert(rows, "step_events", self._step_row(s, shard_id, side, checkpoint_id))

    def write_finding(self, finding: Finding, *, synthetic: bool = False) -> None:
        """Write a finding outside a checkpoint batch, e.g. a run-level finding (4.13)."""
        f = _revalidate(finding)
        self._running(f.run_id)
        with self._batch("finding") as rows:
            self._put_finding(rows, f, synthetic)

    def fill_unreached(self, run_id: str, outcome: str, reason: str | None = None, *, gate: str | None = None,
                       detail: str | None = None) -> list[str]:
        """Write `outcome` for every scoped checkpoint that has none, so no checkpoint is left silent.

        1.10: unreached checkpoints of an aborted run carry its abort reason (env_error with the aborting gate,
        or harness_error); 8.2 and 8.9 use the same for orchestrator_restart_budget, store_write_failed, disk_full.
        """
        run = self._running(run_id)
        filled = []
        for cid in self.missing_outcomes(run_id):
            c = self._one("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (cid,))
            persona = self._one("SELECT persona FROM run_scope WHERE run_id = ? AND checkpoint_id = ?",
                                (run_id, cid))[0]
            self.write_checkpoint(CheckpointOutcome(
                run_id=run["run_id"], checkpoint_id=cid, outcome=outcome, reason=reason, detail=detail, gate=gate,
                workspace_id=c["workspace_id"], persona=persona, data_mode=c["data_mode"], tier=c["tier"],
                flow_id=c["flow_id"], step_id=c["step_id"]))
            filled.append(cid)
        return filled

    # -- reading outcomes, steps, findings ------------------------------------------------------------------
    def _outcome(self, row: sqlite3.Row, record_json: str) -> CheckpointOutcome:
        d = json.loads(record_json)
        rec = _outcome_from_dict(d)
        code, enc = self._encode_reason(rec)
        if (rec.outcome, code, enc) != (row["outcome"], row["reason"], row["reason_detail"]):
            d["outcome"] = row["outcome"]
            d["reason"], d["detail"] = self._decode_reason(row["outcome"], row["reason"], row["reason_detail"])
            for k in ("reason", "detail"):
                if d[k] is None:
                    del d[k]
        d["review_state"] = row["review_state"]
        if decision_int(rec.decision_ref) != row["decision_id"]:
            d["decision_ref"] = decision_ref(row["decision_id"])
        return _outcome_from_dict(d)

    def get_outcome(self, run_id: str, checkpoint_id: str) -> CheckpointOutcome | None:
        row = self._one("SELECT o.*, r.record_json FROM outcomes o JOIN outcome_records r USING (run_id, "
                        "checkpoint_id) WHERE o.run_id = ? AND o.checkpoint_id = ?", (run_id, checkpoint_id))
        return None if row is None else self._outcome(row, row["record_json"])

    def outcomes(self, run_id: str) -> list[CheckpointOutcome]:
        rows = self._conn.execute("SELECT o.*, r.record_json FROM outcomes o JOIN outcome_records r USING (run_id, "
                                  "checkpoint_id) WHERE o.run_id = ? ORDER BY o.checkpoint_id", (run_id,))
        return [self._outcome(r, r["record_json"]) for r in rows]

    def step_outcomes(self, run_id: str) -> list[StepOutcome]:
        rows = self._conn.execute("SELECT * FROM step_events WHERE run_id = ? AND step_outcome IS NOT NULL "
                                  "ORDER BY shard_id, seq", (run_id,))
        return [StepOutcome(run_id=r["run_id"], flow_id=r["flow_id"], step_id=r["step_id"], status=r["step_outcome"],
                            attempt=r["attempt"], locator_rung=r["locator_level"], duration_ms=r["duration_ms"] or 0,
                            finding_ref=(r["reason"][len("finding:"):] if (r["reason"] or "").startswith("finding:")
                                         else None),
                            recorded_at=r["wall_at"]) for r in rows]

    def _finding(self, r: sqlite3.Row) -> Finding:
        return Finding(finding_id=r["natural_key"], run_id=r["run_id"], kind=r["kind"], description=r["description"],
                       first_seen_run_id=r["first_seen_run_id"], checkpoint_id=r["checkpoint_id"], tier=r["tier"],
                       region_id=r["region_id"], primitive=r["primitive"], manifest_entry_ref=r["manifest_entry"],
                       phash=bytes(r["phash"]).hex() if r["phash"] is not None else None, category=r["category"],
                       evidence=json.loads(r["evidence_json"]), triage_state=r["triage_state"],
                       decision_ref=decision_ref(r["decision_id"]), ticket=r["ticket_ref"],
                       created_at=r["created_at"])

    def findings(self, run_id: str) -> list[Finding]:
        return [self._finding(r) for r in self._conn.execute(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY natural_key", (run_id,))]

    def get_finding(self, finding_id: str) -> Finding | None:
        r = self._one("SELECT * FROM findings WHERE natural_key = ?", (finding_id,))
        return None if r is None else self._finding(r)

    # -- the three later changes of 4.13 --------------------------------------------------------------------
    def _outcome_row_for(self, run_id: str, checkpoint_id: str) -> sqlite3.Row:
        row = self._one("SELECT * FROM outcomes WHERE run_id = ? AND checkpoint_id = ?", (run_id, checkpoint_id))
        if row is None:
            raise StoreRefused(f"no outcome for {checkpoint_id} in {run_id}")
        return row

    def set_decision_ref(self, run_id: str, checkpoint_id: str, decision_id: str) -> CheckpointOutcome:
        """First later change (4.13): link the decision that suppresses this row's finding. Set once."""
        row = self._outcome_row_for(run_id, checkpoint_id)
        did = decision_int(decision_id)
        if self._one("SELECT 1 FROM decisions WHERE decision_id = ?", (did,)) is None:
            raise StoreRefused(f"unknown decision {decision_id}")
        if row["decision_id"] != did:
            if row["decision_id"] is not None:
                raise StoreRefused(f"decision_ref is already {decision_ref(row['decision_id'])} (4.13: set once)")
            with self._batch("decision_ref") as rows:
                self._update(rows, "outcomes", {"run_id": run_id, "checkpoint_id": checkpoint_id},
                             {"decision_id": did})
        return self.get_outcome(run_id, checkpoint_id)

    def _pending(self, run_id: str, checkpoint_id: str) -> sqlite3.Row:
        row = self._outcome_row_for(run_id, checkpoint_id)
        if row["outcome"] != "needs_human" or row["review_state"] != "pending":
            raise StoreRefused(f"{checkpoint_id} in {run_id} is not a pending review; outcomes are final (4.13)")
        return row

    def human_decision_on(self, run_id: str, checkpoint_id: str) -> bool:
        """True when a human decision already exists on the item (8.3, F28): inherited by the row, made on a
        finding of the item, or made on this run and checkpoint; revoked and superseded rows do not count."""
        row = self._outcome_row_for(run_id, checkpoint_id)
        if row["decision_id"] is not None:
            return True
        return self._one(
            "SELECT 1 FROM decisions d LEFT JOIN findings f ON f.finding_id = d.source_finding_id "
            "WHERE d.status IN ('active','reopened') AND d.revoked_at IS NULL AND ("
            "(f.run_id = :run AND f.checkpoint_id = :cp) OR (d.run_id = :run AND d.checkpoint_id = :cp))",
            {"run": run_id, "cp": checkpoint_id}) is not None

    def apply_review(self, review: Review, *, outcome: str | None = None, reason: str | None = None
                     ) -> CheckpointOutcome:
        """Second later change (4.13, 8.3, F28): a pending verdict lands.

        The `reviews` row is stored. If a human decision already exists on the item the verdict is an annotation
        only: `review_state` goes to done, the outcome and reason stay. Otherwise the row takes the outcome 3.11
        gives (default: the review's mapped outcome): `intended` or `collateral` with no reason, `noise` only
        through the A/A floor (reason below_floor, never from a verdict alone), or `needs_human` with
        reviewer_no_match or reviewer_uncertain; review_state done with the review id.
        """
        row = self._pending(review.run_id, review.checkpoint_id)
        human = self.human_decision_on(review.run_id, review.checkpoint_id)
        new = outcome or review.mapped_outcome
        if not human:
            allowed = {"intended": (None,), "collateral": (None,), "noise": ("below_floor",),
                       "needs_human": ("reviewer_no_match", "reviewer_uncertain")}
            if new not in allowed or reason not in allowed[new]:
                raise StoreRefused(f"a review cannot end in {new} (reason {reason}); 4.13 allows "
                                   "intended, collateral, noise via A/A, needs_human no_match or uncertain")
            self.taxonomy.check_checkpoint(new, reason)
        if not 0.0 <= review.confidence <= 1.0:
            raise StoreRefused("review confidence is in [0, 1] (3.13)")
        values = dataclasses.asdict(review)
        values["created_at"] = utc(review.created_at)
        key = {"run_id": row["run_id"], "checkpoint_id": row["checkpoint_id"]}
        with self._batch("review") as rows:
            review_id = self._insert(rows, "reviews", values)
            change = {"review_state": "done", "review_id": review_id}
            if not human:
                change = {"outcome": new, "reason": reason, "reason_detail": None, **change}
            self._update(rows, "outcomes", key, change)
        return self.get_outcome(review.run_id, review.checkpoint_id)

    def end_review(self, run_id: str, checkpoint_id: str, reason: str) -> CheckpointOutcome:
        """Third later change (4.13): a pending item that never got a verdict ends its review.

        `reviewer_cap` when the per-run spend ceiling ended the run (`cause: dollar_cap` in reason_detail);
        `reviewer_unavailable` when the Reviewer job gave up. Outcome stays needs_human; no review id.
        """
        row = self._pending(run_id, checkpoint_id)
        if reason not in ("reviewer_cap", "reviewer_unavailable"):
            raise StoreRefused("a review ends without a verdict only as reviewer_cap or reviewer_unavailable (4.13)")
        detail = "cause: dollar_cap" if reason == "reviewer_cap" else (
            row["reason_detail"] if row["reason"] == reason else None)
        with self._batch("review_end") as rows:
            self._update(rows, "outcomes", {"run_id": run_id, "checkpoint_id": checkpoint_id},
                         {"reason": reason, "reason_detail": detail, "review_state": "done"})
        return self.get_outcome(run_id, checkpoint_id)

    # -- decisions (4.14) -------------------------------------------------------------------------------------
    @staticmethod
    def _human(identity: str | None, role: str) -> None:
        if identity is not None and identity.strip().lower() in SERVICE_IDENTITIES:
            raise StoreRefused(f"{role} {identity!r} is not a person: decisions are human-only (4.14, L-DEC-01)")

    def record_decision(self, decision: Decision, *, run_id: str, finding_id: str | None = None,
                        build_id: str | None = None, expires_at: str | None = None,
                        confirmed_by: str | None = None, proposed_by: str | None = None,
                        fixed_in_build_id: str | None = None, baseline_id: int | None = None,
                        mask_ref: str | None = None, cluster_id: str | None = None,
                        supersedes: str | None = None) -> Decision:
        """Write a human decision (4.14, 8.3) and return it as stored (canonical id, effective tolerance, UTC).

        The keyword fields are the 4.14 record fields records.Decision does not carry. With `finding_id` the
        finding takes the decision and the triage state of the 4.14 table. With `supersedes` the old decision is
        marked superseded in the same batch (8.3); a decision is never edited otherwise.
        """
        d = _revalidate(decision)
        self._human(d.decided_by, "decided_by")
        self._human(confirmed_by, "confirmed_by")
        self._human(proposed_by, "proposed_by")
        if not d.reason:
            raise StoreRefused("a decision carries its reason (8.3 decisions.reason)")
        if d.status != "active":
            raise StoreRefused("a new decision is written active; revoke and supersede change status (8.3)")
        did = decision_int(d.decision_id)
        cp = self._one("SELECT tier FROM checkpoints WHERE checkpoint_id = ?", (d.signature.checkpoint_id,))
        if cp is None:
            raise StoreRefused(f"unknown checkpoint {d.signature.checkpoint_id}")
        tolerance = min(d.hamming_tolerance, TIER1_TOLERANCE if cp["tier"] == 1 else DEFAULT_TOLERANCE)
        region = self._one("SELECT g.hamming_tolerance FROM regions g JOIN runs r ON r.config_commit = g.config_commit "
                           "WHERE r.run_id = ? AND g.checkpoint_id = ? AND g.region_id = ?",
                           (run_id, d.signature.checkpoint_id, d.signature.region_id))
        if region is not None and region[0] is not None:          # lowered by calibration, never raised (9.7)
            tolerance = min(tolerance, region[0])
        decided_at = utc(d.decided_at)
        if expires_at is None and d.action in ("noise_signature", "request_mask"):
            expires_at = (datetime.fromisoformat(decided_at) + timedelta(days=EXPIRY_DAYS)).isoformat()
        source_finding = None
        if finding_id is not None:
            f = self._one("SELECT finding_id, synthetic FROM findings WHERE natural_key = ?", (finding_id,))
            if f is None:
                raise StoreRefused(f"unknown finding {finding_id}")
            source_finding = f["finding_id"]
        old = None
        if supersedes is not None:
            old = self._one("SELECT status FROM decisions WHERE decision_id = ?", (decision_int(supersedes),))
            if old is None or old["status"] not in ("active", "reopened"):
                raise StoreRefused(f"{supersedes} is not an active decision to supersede (8.3)")
        triage = (TRIAGE_FOR_ACTION.get(d.action)
                  or ("confirmed_bug" if d.verdict == "confirmed_bug" else "open"))   # reject_baseline (4.14)
        self.taxonomy.check_member("triage_states", triage)
        with self._batch("decision") as rows:
            self._insert(rows, "decisions", {
                "decision_id": did, "checkpoint_id": d.signature.checkpoint_id, "region_id": d.signature.region_id,
                "phash": phash_bytes(d.signature.phash), "hamming_tolerance": tolerance,
                "category": d.signature.category, "action": d.action, "verdict": d.verdict, "source": d.source,
                "decided_by": d.decided_by, "proposed_by": proposed_by, "confirmed_by": confirmed_by,
                "decided_at": decided_at, "run_id": run_id, "build_id": build_id, "reason": d.reason,
                "ticket_ref": d.ticket, "mask_ref": mask_ref, "baseline_id": baseline_id,
                "fixed_in_build_id": fixed_in_build_id, "cluster_id": cluster_id, "expires_at": utc(expires_at),
                "status": "active", "supersedes_decision_id": decision_int(supersedes),
                "source_finding_id": source_finding})
            if supersedes is not None:
                self._update(rows, "decisions", {"decision_id": decision_int(supersedes)}, {"status": "superseded"})
            if source_finding is not None:
                change = {"decision_id": did, "triage_state": triage}
                if d.ticket:
                    change["ticket_ref"] = d.ticket
                self._update(rows, "findings", {"finding_id": source_finding}, change)
        return self.get_decision(decision_ref(did))

    def revoke_decision(self, decision_id: str, *, by: str, reason: str, at: str | None = None) -> Decision:
        """Revoke a decision: the three revoke fields together and status revoked (8.3, 4.14); the row stays."""
        self._human(by, "revoked_by")
        if not reason:
            raise StoreRefused("a revoke carries its reason (8.3)")
        did = decision_int(decision_id)
        row = self._one("SELECT status FROM decisions WHERE decision_id = ?", (did,))
        if row is None:
            raise StoreRefused(f"unknown decision {decision_id}")
        if row["status"] == "revoked":
            raise StoreRefused(f"{decision_id} is already revoked")
        with self._batch("decision_revoke") as rows:
            self._update(rows, "decisions", {"decision_id": did},
                         {"status": "revoked", "revoked_at": utc(at) or now(), "revoked_by": by,
                          "revoke_reason": reason})
        return self.get_decision(decision_id)

    def get_decision(self, decision_id: str) -> Decision | None:
        r = self._one("SELECT * FROM decisions WHERE decision_id = ?", (decision_int(decision_id),))
        return None if r is None else _decision_from_row(r)

    def lookup_decisions(self, signature: Signature, *, at: str | None = None, rescued: bool = False,
                         synthetic: bool = False) -> DecisionLookup:
        """The 8.3 signature lookup on this database (see the module-level `lookup_decisions`)."""
        return lookup_decisions(self._conn, signature, at=at, rescued=rescued, synthetic=synthetic)

    # -- retention (8.6) -------------------------------------------------------------------------------------
    def deletable(self, sha256: str) -> bool:
        """8.6: never a pinned object, one a baseline row names, or one a finding not closed references."""
        art = self._one("SELECT pinned, deleted_at FROM artifacts WHERE sha256 = ?", (sha256,))
        if art is None or art["pinned"] or art["deleted_at"] is not None:
            return False
        if self._one("SELECT 1 FROM baselines WHERE image_sha256 = ?1 OR sidecar_sha256 = ?1", (sha256,)):
            return False
        return self._one("SELECT 1 FROM findings WHERE triage_state <> 'closed' AND instr(evidence_json, ?) > 0",
                         (sha256,)) is None

    def sweep(self, artifacts: ArtifactStore, candidates: Iterable[str], *, backup_ok: bool,
              at: str | None = None) -> SweepResult:
        """Remove the candidates 8.6 allows from `artifacts`: deleted_at is set before the file is unlinked.

        Choosing candidates by class and age is the sweep job's; this enforces the safety rules. Nothing runs on
        a night whose backup failed (8.6: "retention skipped: backup failed").
        """
        if not backup_ok:
            return SweepResult(skipped=True)
        deleted, kept = [], []
        for sha in candidates:
            if not self.deletable(sha):
                kept.append(sha)
                continue
            with self._batch("retention") as rows:
                self._update(rows, "artifacts", {"sha256": sha}, {"deleted_at": utc(at) or now()})
            artifacts.unlink(sha)
            deleted.append(sha)
        return SweepResult(tuple(deleted), tuple(kept))
