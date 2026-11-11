"""The plaintext secret scan (6.9 Plaintext scan, 8.4, 8.9): a hash cannot find a substring, so the plaintext of
each secret the run used is searched for in everything the run wrote.

- `SecretScanner(secrets)` holds the plaintexts by reference (`login.password` -> value) and finds them in bytes
  and text. Bytes are searched as UTF-8, UTF-16-LE and UTF-16-BE, and also in the JSON-escaped and HTML-escaped
  forms a ledger or a report would write; text (SQLite values) in the plain and escaped forms. A hit names the
  reference, the place and the form, never the value. `refs_in` is the `ArtifactStore` put hook (each text
  artifact is scanned before it is stored or uploaded) and `check_upload` the same check for a results batch.
- `scan_run(db, artifacts, reports, secrets, run_id=...)` is the teardown scan: the run's SQLite rows (every
  table with a `run_id` or `first_run_id` column, rows of this run; the step event log among them), every file of
  the run's artifact set (objects, manifest, ledgers and journals) and every file or directory in `reports` (report
  HTML, results.jsonl, Reviewer requests and agent transcripts, crash files and log slices kept elsewhere). It
  returns a `ControlResult`: passed; `aborted_harness (reason: secret_leaked)` with the report withheld, the
  credential to rotate, an S1 incident and one `secret_leaked` finding per secret, after moving the set to
  quarantine when a quarantine root is given (8.4); or `aborted_harness (reason: secret_scan_unavailable)` with
  the report withheld when the secrets cannot be had under a lease (`secrets=None`) or the scan cannot finish.

The plaintexts come from the Orchestrator's locked buffer (6.9; `VirtualLock` is Windows). A value passed as a
`bytearray` is zeroed when the scan ends, as are the scanner's own search buffers; Python's immutable copies of a
`str` cannot be zeroed and are released for collection.

Choices where 6.9 is silent (reported):

1. "SQLite rows written this run" are the rows of every table with a `run_id` (or, for `artifacts`, a
   `first_run_id`) equal to the run; rows of tables without either are covered by the shard ledger
   (results.jsonl holds every row the shard wrote, 8.4), which the caller passes in `reports` or keeps in the set.
2. Image and video files (recognised by their leading bytes) are not scanned: images pass the redaction stage and
   carry its version (8.4: text artifacts pass the plaintext scan "instead"), and the typed password field is
   masked, so pixels are not the leak path (6.9).
3. A scan that starts but cannot finish (an unreadable file, a failed query) is `secret_scan_unavailable`, like
   one that cannot start: the report stays withheld until a human runs the scan with a fresh lease.
4. An empty mapping of secrets means the run used none: the scan passes with nothing to search for.
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from ..orchestrator import alerts
from ..orchestrator.results import ControlResult, FindingSpec, harness_abort_outcome
from .artifacts import ArtifactStore, has_image_magic
from .store import StoreError

CHUNK = 8 * 1024 * 1024
_BYTE_FORMS = ("utf-8", "utf-16-le", "utf-16-be")


@dataclass(frozen=True)
class SecretHit:
    """One place a secret's plaintext was found: the secret's reference, the place, and the encoding or escape."""
    ref: str
    location: str
    form: str


def _text_forms(value: str) -> dict[str, str]:
    forms = {"plain": value, "json": json.dumps(value)[1:-1],
             "json-unicode": json.dumps(value, ensure_ascii=False)[1:-1], "html": html.escape(value, quote=True)}
    out: dict[str, str] = {}
    for name, text in forms.items():
        if text not in out.values():
            out[name] = text
    return out


class SecretScanner:
    """Finds the plaintext of each secret in bytes, text and files (module docstring)."""

    def __init__(self, secrets: Mapping[str, str | bytes | bytearray]):
        self._texts: list[tuple[str, str, str]] = []
        self._needles: list[tuple[str, str, bytearray]] = []
        for ref, value in secrets.items():
            if not ref or not isinstance(ref, str):
                raise ValueError("a secret is named by its vault reference (4.7 `secret: vault_ref`)")
            text = value if isinstance(value, str) else bytes(value).decode("utf-8")
            if not text:
                raise ValueError(f"secret {ref} is empty: an empty plaintext matches everything")
            for form, t in _text_forms(text).items():
                self._texts.append((ref, form, t))
                encodings = _BYTE_FORMS if form == "plain" else ("utf-8",)
                for enc in encodings:
                    needle = bytearray(t.encode(enc))
                    if all(n != needle for _, _, n in self._needles):
                        self._needles.append((ref, form if form != "plain" else enc, needle))
        self.overlap = max((len(n) for _, _, n in self._needles), default=1) - 1

    def scan_bytes(self, data: bytes | bytearray | memoryview, location: str) -> list[SecretHit]:
        """Every (secret, form) found in `data`."""
        buf = data if isinstance(data, bytes) else bytes(data)
        return [SecretHit(ref, location, form) for ref, form, n in self._needles if buf.find(n) >= 0]

    def scan_text(self, text: str, location: str) -> list[SecretHit]:
        """Every (secret, form) found in `text` (a decoded value, such as an SQLite text column)."""
        return [SecretHit(ref, location, form) for ref, form, t in self._texts if t in text]

    def refs_in(self, data: bytes) -> tuple[str, ...]:
        """The `ArtifactStore` put hook: the references found in `data`, sorted."""
        return tuple(sorted({h.ref for h in self.scan_bytes(data, "")}))

    def scan_file(self, path: str | os.PathLike, location: str) -> list[SecretHit]:
        """Scan a file in chunks that overlap by the longest search form, so no hit straddles a boundary."""
        found: dict[tuple[str, str], SecretHit] = {}
        tail = b""
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                window = tail + chunk
                for h in self.scan_bytes(window, location):
                    found.setdefault((h.ref, h.form), h)
                tail = window[-self.overlap:] if self.overlap else b""
        return list(found.values())

    def zero(self) -> None:
        """Overwrite the search buffers (6.9: the buffer is zeroed when the scan ends)."""
        for _, _, n in self._needles:
            n[:] = bytes(len(n))
        self._needles.clear()
        self._texts.clear()


def _query(db: Any, sql: str, args: Sequence[Any] = ()) -> list[Sequence[Any]]:
    q = getattr(db, "query", None)
    return list(q(sql, args)) if callable(q) else db.execute(sql, args).fetchall()


def _db_hits(scanner: SecretScanner, db: Any, run_id: str) -> tuple[list[SecretHit], int]:
    """Choice 1: the rows of this run in every table that names its run."""
    hits: list[SecretHit] = []
    rows_seen = 0
    tables = _query(db, "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                        "ORDER BY name")
    for name, ddl in tables:
        cols = [r[1] for r in _query(db, f'PRAGMA table_info("{name}")')]
        key = "run_id" if "run_id" in cols else "first_run_id" if "first_run_id" in cols else None
        if key is None:
            continue
        rowid = "WITHOUT ROWID" not in (ddl or "").upper()
        select = ", ".join((["rowid"] if rowid else []) + [f'"{c}"' for c in cols])
        for i, row in enumerate(_query(db, f'SELECT {select} FROM "{name}" WHERE "{key}" = ?', (run_id,))):
            rows_seen += 1
            ident = row[0] if rowid else i + 1
            values = row[1:] if rowid else row
            for col, value in zip(cols, values):
                where = f"db:{name}[{ident}].{col}"
                if isinstance(value, str):
                    hits += scanner.scan_text(value, where)
                elif isinstance(value, (bytes, bytearray, memoryview)):
                    hits += scanner.scan_bytes(bytes(value), where)
    return hits, rows_seen


def _files(path: Path) -> Iterator[Path]:
    if path.is_file():
        yield path
    elif path.is_dir():
        yield from sorted(p for p in path.rglob("*") if p.is_file())
    else:
        raise FileNotFoundError(f"{path} does not exist")


def _file_hits(scanner: SecretScanner, roots: Sequence[Path]) -> tuple[list[SecretHit], int, int]:
    hits: list[SecretHit] = []
    files = size = 0
    for root in roots:
        for p in _files(root):
            with open(p, "rb") as f:
                if has_image_magic(f.read(16)):
                    continue                                    # choice 2
            files += 1
            size += p.stat().st_size
            hits += scanner.scan_file(p, f"file:{p}")
    return hits, files, size


def unavailable_result(detail: str) -> ControlResult:
    """`aborted_harness (reason: secret_scan_unavailable)`: the report is withheld until a human scans (6.9)."""
    run, un = harness_abort_outcome("secret_scan_unavailable")
    return ControlResult("secret_scan", False, "teardown", run=run, unreached=un, alert=alerts.aborted_harness(),
                         actions=("withhold_report",), detail=detail)


def leak_result(hits: Sequence[SecretHit], *, quarantined_to: str | None = None,
                evidence: Mapping[str, Any] | None = None) -> ControlResult:
    """`aborted_harness (reason: secret_leaked)` for these hits (6.9, 8.9): report withheld, credential rotated, S1
    incident with the security contact and the system owner told, one `secret_leaked` finding per secret."""
    by_ref: dict[str, list[str]] = {}
    for h in hits:
        by_ref.setdefault(h.ref, [])
        if h.location not in by_ref[h.ref]:
            by_ref[h.ref].append(h.location)
    findings = tuple(FindingSpec("secret_leaked", f"plaintext of secret {ref} found in {len(locs)} place(s)",
                                 severity="S1", detail="; ".join(locs)) for ref, locs in sorted(by_ref.items()))
    run, un = harness_abort_outcome("secret_leaked")
    where = f", objects quarantined to {quarantined_to}" if quarantined_to else ""
    return ControlResult(
        "secret_scan", False, "teardown", run=run, unreached=un, alert=alerts.safety_abort(),
        actions=("withhold_report", "rotate_credential"), findings=findings, incident="S1",
        detail=f"plaintext secret found: report withheld{where}",
        evidence={**(evidence or {}), "hits": [{"ref": h.ref, "location": h.location, "form": h.form} for h in hits],
                  "quarantined_to": quarantined_to})


def check_upload(scanner: SecretScanner, data: bytes, *, location: str) -> ControlResult | None:
    """The mid-run check of one results batch or text artifact before upload (6.9): None when clean, else the
    `secret_leaked` result, so uploads stop at once."""
    hits = scanner.scan_bytes(data, location)
    return leak_result(hits) if hits else None


def scan_run(db: Any, artifacts: ArtifactStore, reports: Sequence[str | os.PathLike],
             secrets: Mapping[str, str | bytes | bytearray] | None, *, run_id: str,
             quarantine_root: str | os.PathLike | None = None, at: str | None = None) -> ControlResult:
    """The teardown plaintext scan of one run (module docstring). `db` is the `store.Store` (or a read connection);
    `artifacts` the run's set; `reports` further files or directories; `secrets` maps each secret reference used
    to its plaintext, or is None when no lease can be had. With `quarantine_root`, a hit moves the set there and
    marks its `artifact_sets` rows (when `db` is the store)."""
    if secrets is None:
        return unavailable_result("no secret could be read under a lease: report withheld until a human runs the "
                                  "scan with a fresh lease")
    try:
        scanner = SecretScanner(secrets)
        try:
            db_hits, rows = _db_hits(scanner, db, run_id)
            file_hits, files, size = _file_hits(scanner, [artifacts.root, *map(Path, reports)])
        except (OSError, sqlite3.Error, StoreError) as e:          # choice 3
            return unavailable_result(f"the scan could not finish ({type(e).__name__}): report withheld until a "
                                      "human runs it")
        finally:
            scanner.zero()
    finally:
        for value in secrets.values():
            if isinstance(value, bytearray):
                value[:] = bytes(len(value))
    stats = {"rows": rows, "files": files, "bytes": size}
    hits = db_hits + file_hits
    if not hits:
        return ControlResult("secret_scan", True, "teardown", evidence=stats)
    store = db if hasattr(db, "batch") else None
    dest = artifacts.quarantine(run_id, quarantine_root, store=store, at=at) if quarantine_root is not None else None
    return leak_result(hits, quarantined_to=str(dest) if dest else None, evidence=stats)
