"""The Reporter service's local API on the control host (5.1, 5.12, 5.13, 8.2, 8.7, 8.9): the only path by which
anything changes the Central Store.

`make_service(writer, importer, queue, ...)` returns a `ReporterService`, a WSGI application. Every write runs through
`writer.submit(fn)`, which calls `fn(store)` on the Central Store's single writer thread (8.2); the shard import is
the injected `importer`, the run queue (`orchestrator.queue.RunQueue`) the injected `queue`. Endpoints:

| Method and path | What it does |
|---|---|
| `GET /v1/health` | the report UI's health check: down means the read-only banner (8.2, 9.1) |
| `POST /v1/runs/{run_id}/started` | run-started event: the dead-man timer watches the run (5.13, 5.14) |
| `POST /v1/runs/{run_id}/scope` | the scope manifest at `running` entry (5.12): checked against `run_scope`, kept in the run summary |
| `POST /v1/runs/{run_id}/results` | one checkpoint batch as it completes (5.1): outcome, steps, findings, the artifacts it names |
| `POST /v1/runs/{run_id}/completed` | run-completed event: the dead-man timer stops watching (5.14) |
| `POST /v1/shards/{shard_id}/ship` | the shard manifest at `reported` (8.2); the answer lists the objects still needed |
| `PUT /v1/artifacts/{sha256}` | one redacted object (8.2): its hash recomputed, its redaction version required for a picture |
| `POST /v1/rigs/{rig_id}/heartbeat` | the rig heartbeat summary (5.1) |
| `POST /v1/webhook` | the pipeline webhook (PR, merge, RC builds) into the run queue (5.1, 11.2) |
| `POST /v1/actions/{action}` | a report UI action with the SSO identity (8.7, 9.6): `report.actions.apply` |

Answers are JSON. Status codes: 200 done (a replay of a stored item is done too: every endpoint is idempotent),
400 a malformed request, 403 an action refused by the 9.6 table, 404 an unknown run or shard, 409 an import conflict
or an active decision (8.9, 9.7), 422 a record or object refused (hash mismatch, redaction version missing,
taxonomy, a constraint: the body names it).

The WSGI toolkit (`Request`, `Router`, `respond`) is stdlib only: 5.18 names FastAPI with uvicorn, but the package
takes no new dependency, so the service and the report UI (`ui.app`) are plain WSGI applications any WSGI server
(`wsgiref` for one process, or a production server) can host.

Choices where the spec is ambiguous or silent (reported to the coordinator):
- Uploads land in `incoming/<shard_id>/` (an `ArtifactStore` set named by the shard) with an `index.json` of what
  arrived and the hash mismatches per object; a results batch copies the objects it names into `runs/<run_id>/` (the
  copy re-verifies the hash) and writes their artifact rows with the batch. The shard import moves the rest.
- "Re-requested up to 3 times" (8.9): the first three mismatches of an object answer 422 with `re_request: true`; the
  fourth fails the shard: state `import_failed`, every silent scoped checkpoint `harness_error (reason:
  artifact_persist_failed)` and the run `aborted_harness (reason: artifact_persist_failed)` with `upload_hash_mismatch`
  in the abort facts, last gate `reported` (the hand-off). Later uploads for that shard answer 409.
- A picture without a redaction version is refused (422) and, while the run is open and the named checkpoint has no
  outcome, the checkpoint is written `harness_error (reason: redaction_version_missing)` (5.12, 8.9).
- The scope manifest is checked against the scope the run was created with (`run_scope`, persona included); its
  hash, size and time are kept under `summary_json.scope_manifests[shard_id]`, which is also how the dead-man timer
  knows after a restart that the run reached `running`. The same manifest again is a replay; another one is 409.
- The shard is imported when its manifest and every object it lists are in: on the ship call when nothing is
  missing, else on the last PUT. The ship call sets the shard `shipping` (8.3).
- An action's outbox payloads go to `payload_sink(sha256_id, bytes)`, by default the artifact set `outbox/` under the
  artifact root, content addressed, so the outbox worker can read a job's payload by its hash.
- Identity: the report UI forwards the SSO login in the action body; the local API is reachable only on the control
  host (8.7), so it trusts its caller for the identity and the 9.6 table decides what that login may do.
"""
from __future__ import annotations

import dataclasses
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, TypeVar
from urllib.parse import parse_qs

from ..canonical import sha256_id
from ..orchestrator.queue import RunFinished, RunReachedRunning, WebhookError, webhook_to_request
from ..records import CheckpointOutcome, Finding, RunOutcome, StepOutcome, from_dict
from ..report.actions import ActionContext, ActionRefused, ActionRequest, apply as apply_action
from ..store.artifacts import (SHA256_RE, ArtifactError, ArtifactHashMismatch, ArtifactStore, Redaction,
                               RedactionRefused, SecretLeaked, StoredArtifact, TextScanner)
from ..store.mirror import MaskBudgetExceeded
from ..store.store import ActiveDecisionExists, ImportConflict, Store, StoreError, StoreRefused
from ..taxonomy import TaxonomyError
from .deadman import DeadmanTimer, Fired, fire

T = TypeVar("T")
MAX_REHASH = 3                      # 8.9: an upload hash mismatch is re-requested up to 3 times
REASONS = {200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 410: "Gone", 422: "Unprocessable Entity",
           500: "Internal Server Error", 503: "Service Unavailable"}


# ---------------------------------------------------------------------------------------------------------------
# the WSGI toolkit (shared with ui.app)

class Request:
    """One WSGI request: method, path, query, headers and body."""

    def __init__(self, environ: Mapping[str, Any]):
        self.environ = environ
        self.method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        self.path = str(environ.get("PATH_INFO", "/")) or "/"
        self.query = {k: v[0] for k, v in parse_qs(str(environ.get("QUERY_STRING", ""))).items()}
        self.params: dict[str, str] = {}
        self._body: bytes | None = None

    def header(self, name: str) -> str | None:
        key = name.upper().replace("-", "_")
        if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            return self.environ.get(key) or None
        return self.environ.get(f"HTTP_{key}") or None

    @property
    def body(self) -> bytes:
        if self._body is None:
            try:
                n = int(self.environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                n = 0
            self._body = self.environ["wsgi.input"].read(n) if n > 0 else b""
        return self._body

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError as e:
            raise BadRequest(f"the body is not JSON: {e}") from e

    def form(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.body.decode("utf-8")).items()}


class BadRequest(ValueError):
    """A request the API cannot read (400)."""


Response = tuple[int, list[tuple[str, str]], bytes]
Handler = Callable[[Request], Response]


def respond(status: int, body: Any = None, *, content_type: str = "application/json",
            headers: Iterable[tuple[str, str]] = ()) -> Response:
    """A response: JSON for a mapping or list, the bytes or text as given otherwise."""
    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif isinstance(body, str):
        data = body.encode("utf-8")
    elif body is None and status == 204:
        data = b""
    else:
        data = json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    return status, [("Content-Type", content_type), ("Content-Length", str(len(data))), *headers], data


class Router:
    """Routes `METHOD /path/{name}` patterns to handlers; `{name}` matches one segment, `{name:path}` the rest."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        def part(m: re.Match[str]) -> str:
            name, _, kind = m.group(1).partition(":")
            return f"(?P<{name}>.+)" if kind == "path" else f"(?P<{name}>[^/]+)"
        regex = re.compile("^" + re.sub(r"\{([^}]+)\}", part, pattern) + "$")
        self.routes.append((method.upper(), regex, handler))

    def dispatch(self, req: Request) -> Response:
        allowed = False
        for method, regex, handler in self.routes:
            m = regex.match(req.path)
            if m is None:
                continue
            allowed = True
            if method == req.method:
                req.params = m.groupdict()
                return handler(req)
        return respond(405 if allowed else 404, {"error": "method not allowed" if allowed else "not found"})

    def wsgi(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        req = Request(environ)
        try:
            status, headers, body = self.dispatch(req)
        except BadRequest as e:
            status, headers, body = respond(400, {"error": str(e)})
        start_response(f"{status} {REASONS.get(status, 'Unknown')}", headers)
        return [body]


# ---------------------------------------------------------------------------------------------------------------
# the injected collaborators

class Writer(Protocol):
    """The Central Store's single writer (8.2): runs `fn(store)` on its thread and returns the result or raises."""

    def submit(self, fn: Callable[[Store], T]) -> T:
        """Run one unit of writer work."""


class InlineWriter:
    """A writer that runs the work on the calling thread under a lock: the single writer of a one-process service
    (a single-threaded WSGI server) and of tests. A threaded server uses the store package's writer service."""

    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.Lock()

    def submit(self, fn: Callable[[Store], T]) -> T:
        with self._lock:
            return fn(self.store)


class ShardImporter(Protocol):
    """The 8.2 shard import: verify the shipped Run Store against its ledger, import the rows in one transaction,
    move the objects of `incoming_dir` into `runs/<run_id>/` and mark the shard imported (or import_failed)."""

    def import_shard(self, store: Store, incoming_dir: Path, run_id: str, shard_id: str) -> Any:
        """Import one shipped shard; raise `StoreRefused` (or `ImportConflict`) when it cannot be imported."""


class Queue(Protocol):
    """The run queue (`orchestrator.queue.RunQueue`)."""

    def handle(self, event: Any) -> list[Any]:
        """Apply one event; returns the outputs to act on."""


# ---------------------------------------------------------------------------------------------------------------
# the service

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ReporterService:
    """The local API (module docstring). Call `tick(now)` from a timer (`deadman.start_timer`) for the dead-man."""

    def __init__(self, writer: Writer, importer: ShardImporter | None, queue: Queue | None, *,
                 artifact_root: str | Path, incoming_root: str | Path | None = None,
                 actions: ActionContext | None = None, timer: DeadmanTimer | None = None,
                 on_queue: Callable[[list[Any]], None] | None = None,
                 payload_sink: Callable[[str, bytes], None] | None = None, scanner: TextScanner | None = None,
                 require_local: bool = True, clock: Callable[[], datetime] = _utc_now):
        self.writer = writer
        self.importer = importer
        self.queue = queue
        self.artifact_root = Path(artifact_root)
        self.incoming_root = Path(incoming_root) if incoming_root else self.artifact_root / "incoming"
        self.actions = actions
        self.timer = timer or DeadmanTimer()
        self.on_queue = on_queue
        self.payload_sink = payload_sink or self._store_payload
        self.scanner = scanner
        self.require_local = require_local
        self.clock = clock
        self._lock = threading.RLock()                 # the incoming index; re-entered by the import
        self.tick_errors: list[tuple[str, str]] = []   # dead-man closes the writer refused (retried next tick)
        r = self.router = Router()
        r.add("GET", "/v1/health", self.health)
        r.add("POST", "/v1/runs/{run_id}/started", self.run_started)
        r.add("POST", "/v1/runs/{run_id}/scope", self.scope_manifest)
        r.add("POST", "/v1/runs/{run_id}/results", self.results)
        r.add("POST", "/v1/runs/{run_id}/completed", self.run_completed)
        r.add("POST", "/v1/shards/{shard_id:path}/ship", self.ship)
        r.add("PUT", "/v1/artifacts/{sha256}", self.put_artifact)
        r.add("POST", "/v1/rigs/{rig_id}/heartbeat", self.heartbeat)
        r.add("POST", "/v1/webhook", self.webhook)
        r.add("POST", "/v1/actions/{action}", self.action)

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        return self.router.wsgi(environ, start_response)

    # -- helpers ------------------------------------------------------------------------------------------------
    def _set(self, root: Path, set_path: str) -> ArtifactStore:
        return ArtifactStore.at(root, set_path, scanner=self.scanner, require_local=self.require_local)

    def _incoming(self, shard_id: str) -> ArtifactStore:
        return ArtifactStore(self.incoming_root / shard_id, shard_id, scanner=self.scanner,
                             require_local=self.require_local)

    def _index_path(self, shard_id: str) -> Path:
        return self.incoming_root / shard_id / "index.json"

    def _index(self, shard_id: str) -> dict:
        p = self._index_path(shard_id)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {"uploads": {}, "mismatches": {}, "ship": None, "failed": False, "imported": False}

    def _save_index(self, shard_id: str, index: dict) -> None:
        p = self._index_path(shard_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(".index.json.tmp")
        tmp.write_text(json.dumps(index, sort_keys=True, indent=1), encoding="utf-8")
        tmp.replace(p)

    def _store_payload(self, sha_id: str, data: bytes) -> None:
        self._set(self.artifact_root, "outbox").put(data, name=sha_id, kind="request", media_type="application/json")

    def _refusal(self, e: Exception) -> Response:
        if isinstance(e, ActionRefused):
            return respond(403, {"refused": str(e), "clause": e.clause})
        if isinstance(e, MaskBudgetExceeded):
            return respond(422, {"refused": str(e), "clause": "L-DEC-04", "panel": e.panel, "share": e.current,
                                 "budget": e.budget, "mask_share": e.mask_share})
        if isinstance(e, ActiveDecisionExists):
            return respond(409, {"refused": str(e), "active": [d.decision_id for d in e.active]})
        if isinstance(e, ImportConflict):
            return respond(409, {"refused": str(e)})
        return respond(422, {"refused": str(e)})

    def _run(self, run_id: str) -> Any:
        rows = self.writer.submit(lambda s: s.query("SELECT * FROM runs WHERE run_id = ?", (run_id,)))
        return rows[0] if rows else None

    # -- endpoints ----------------------------------------------------------------------------------------------
    def health(self, req: Request) -> Response:
        """Up when the writer answers a read (the UI shows the read-only banner otherwise, 8.2)."""
        try:
            self.writer.submit(lambda s: s.query("SELECT 1"))
        except (StoreError, OSError) as e:
            return respond(503, {"ok": False, "error": str(e)})
        return respond(200, {"ok": True})

    def run_started(self, req: Request) -> Response:
        doc = req.json() or {}
        try:
            deadline = datetime.fromisoformat(str(doc["deadline"]))
            w = self.timer.run_started(req.params["run_id"], str(doc["run_type"]), deadline,
                                       shards={str(k): str(v) for k, v in (doc.get("shards") or {}).items()})
        except (KeyError, ValueError) as e:
            raise BadRequest(f"run-started needs run_type and an ISO deadline: {e}") from e
        return respond(200, {"run_id": w.run_id, "dead_man_at": w.deadline.isoformat(timespec="seconds")})

    def scope_manifest(self, req: Request) -> Response:
        """The scope manifest at `running` entry (5.12)."""
        run_id = req.params["run_id"]
        doc = req.json() or {}
        shard = doc.get("shard_id")
        entries = doc.get("checkpoints")
        if not shard or not isinstance(entries, list):
            raise BadRequest("a scope manifest names its shard_id and its checkpoints")
        pairs = sorted((str(e["checkpoint_id"]), str(e["persona"])) for e in entries)
        sha = sha256_id({"shard_id": shard, "checkpoints": pairs})
        at = doc.get("at") or self.clock().isoformat(timespec="seconds")
        stored: list[bool] = []

        def write(s: Store) -> Response:
            run = s.query("SELECT lifecycle, summary_json FROM runs WHERE run_id = ?", (run_id,))
            if not run:
                return respond(404, {"error": f"unknown run {run_id}"})
            if not s.query("SELECT 1 FROM shards WHERE shard_id = ? AND run_id = ?", (shard, run_id)):
                return respond(404, {"error": f"{shard} is not a shard of run {run_id}"})
            manifests = (json.loads(run[0]["summary_json"] or "{}")).get("scope_manifests") or {}
            if shard in manifests:
                if manifests[shard]["sha256"] == sha:
                    return respond(200, {"status": "duplicate", "sha256": sha})
                return respond(409, {"refused": f"{shard} already uploaded another scope manifest (5.12)"})
            if run[0]["lifecycle"] != "running":
                return respond(409, {"refused": f"run {run_id} is {run[0]['lifecycle']}"})
            scoped = {r["checkpoint_id"]: r["persona"] for r in s.query(
                "SELECT checkpoint_id, persona FROM run_scope WHERE run_id = ?", (run_id,))}
            wrong = [c for c, p in pairs if scoped.get(c) != p]
            if wrong:
                return respond(409, {"refused": f"{len(wrong)} checkpoint(s) not in the run's scope as given, first "
                                                f"{wrong[0]} (8.3 run_scope)", "unknown": wrong})
            s.update_summary(run_id, {"scope_manifests": {**manifests, shard: {
                "sha256": sha, "checkpoints": len(pairs), "uploaded_at": at}}})
            stored.append(True)
            return respond(200, {"status": "stored", "sha256": sha})

        out = self.writer.submit(write)
        if stored:
            self.timer.reached_running(run_id)
            self._queue(RunReachedRunning(run_id))
        return out

    def results(self, req: Request) -> Response:
        """One checkpoint batch (5.1): written once; the same batch again is a replay, another one a conflict."""
        run_id = req.params["run_id"]
        doc = req.json() or {}
        shard = doc.get("shard_id")
        try:
            outcome = from_dict(CheckpointOutcome, doc["outcome"])
            steps = [from_dict(StepOutcome, x) for x in doc.get("steps") or ()]
            findings = [from_dict(Finding, x) for x in doc.get("findings") or ()]
        except (KeyError, TypeError, TaxonomyError, ValueError) as e:
            return respond(422, {"refused": f"the batch is not a valid 4.13 record: {e}"})
        if outcome.run_id != run_id or any(f.run_id != run_id for f in findings):
            return respond(422, {"refused": f"a results batch carries rows of run {run_id} only"})
        index = self._index(shard) if shard else {"uploads": {}}
        try:
            arts = [self._central_copy(run_id, shard, index, sha) for sha in doc.get("artifacts") or ()]
        except ArtifactError as e:
            return respond(422, {"refused": str(e)})

        def write(s: Store) -> Response:
            s.write_checkpoint(outcome, steps=steps, findings=findings, artifacts=arts, shard_id=shard,
                               step_side=doc.get("side"))
            return respond(200, {"status": "ok", "checkpoint_id": outcome.checkpoint_id})

        try:
            return self.writer.submit(write)
        except (StoreRefused, TaxonomyError) as e:
            return self._refusal(e)

    def _central_copy(self, run_id: str, shard: str | None, index: dict, sha: str) -> StoredArtifact:
        """Copy an uploaded object into the run's central set, re-verifying its hash (8.2, 8.4)."""
        meta = (index.get("uploads") or {}).get(sha)
        if shard is None or meta is None:
            raise ArtifactError(f"artifact {sha} was not uploaded for this shard; PUT it first (8.2)")
        data = self._incoming(shard).get(sha)
        redaction = Redaction(meta["redaction_version"]) if meta.get("redaction_version") else None
        return self._set(self.artifact_root, f"runs/{run_id}").put(
            data, name=meta.get("name") or sha, kind=meta["kind"], media_type=meta["media_type"],
            redaction=redaction, expected_sha256=sha, checkpoint_id=meta.get("checkpoint_id"),
            side=meta.get("side"))

    def run_completed(self, req: Request) -> Response:
        doc = req.json() or {}
        run_id = req.params["run_id"]
        self.timer.completed(run_id)
        if doc.get("status"):
            self._queue(RunFinished(run_id, str(doc["status"]), bool(doc.get("canaries_passed"))))
        return respond(200, {"run_id": run_id, "watched": False})

    def heartbeat(self, req: Request) -> Response:
        doc = req.json() or {}
        try:
            at = datetime.fromisoformat(str(doc.get("at"))) if doc.get("at") else self.clock()
        except ValueError as e:
            raise BadRequest(f"heartbeat time: {e}") from e
        self.timer.heartbeat(req.params["rig_id"], at)
        return respond(200, {"rig_id": req.params["rig_id"]})

    def ship(self, req: Request) -> Response:
        """The shard manifest (8.2): kept with the shard's uploads; answers what is still needed."""
        shard = req.params["shard_id"]
        doc = req.json() or {}
        run_id = doc.get("run_id")
        objects = doc.get("objects")
        if not run_id or not isinstance(objects, list) or any(
                not SHA256_RE.match(str(o.get("sha256", ""))) for o in objects):
            raise BadRequest("a ship manifest names its run_id and its objects by sha256")
        known = self.writer.submit(lambda s: s.query("SELECT state FROM shards WHERE shard_id = ? AND run_id = ?",
                                                     (shard, run_id)))
        if not known:
            return respond(404, {"error": f"{shard} is not a shard of run {run_id}"})
        with self._lock:
            index = self._index(shard)
            if index["failed"]:
                return respond(409, {"refused": f"{shard} is import_failed"})
            manifest = {k: doc.get(k) for k in ("run_id", "objects", "run_store_sha256", "ledger_sha256",
                                                "ledger_rows")}
            if index["ship"] is not None and index["ship"] != manifest:
                return respond(409, {"refused": f"{shard} was shipped with another manifest (8.2)"})
            index["ship"] = manifest
            self._save_index(shard, index)
        if known[0]["state"] in ("running", "finished"):
            self.writer.submit(lambda s: s.update("shards", {"shard_id": shard}, {"state": "shipping"}))
        return self._maybe_import(shard)

    def _need(self, index: dict) -> list[str]:
        return sorted({o["sha256"] for o in (index["ship"] or {}).get("objects") or ()} - set(index["uploads"]))

    def _maybe_import(self, shard: str) -> Response:
        with self._lock:
            index = self._index(shard)
            need = self._need(index) if index["ship"] is not None else None
            if need or need is None or index["imported"]:
                return respond(200, {"shard_id": shard, "need": need or [], "imported": index["imported"]})
            run_id = index["ship"]["run_id"]
        if self.importer is None:
            return respond(200, {"shard_id": shard, "need": [], "imported": False})
        try:
            self.writer.submit(lambda s: self.importer.import_shard(s, self.incoming_root / shard, run_id, shard))
        except (StoreRefused, TaxonomyError) as e:
            return self._refusal(e)
        with self._lock:
            index = self._index(shard)
            index["imported"] = True
            self._save_index(shard, index)
        return respond(200, {"shard_id": shard, "need": [], "imported": True})

    def put_artifact(self, req: Request) -> Response:
        """One redacted object (8.2): hash recomputed, a picture's redaction version required, idempotent."""
        sha = req.params["sha256"]
        if not SHA256_RE.match(sha):
            raise BadRequest(f"not a sha256: {sha}")
        meta = {"kind": req.header("X-Sentinel-Kind"), "media_type": req.header("Content-Type"),
                "redaction_version": req.header("X-Sentinel-Redaction-Version"),
                "run_id": req.header("X-Sentinel-Run-Id"), "checkpoint_id": req.header("X-Sentinel-Checkpoint-Id"),
                "name": req.header("X-Sentinel-Name") or sha, "side": req.header("X-Sentinel-Side")}
        shard = req.header("X-Sentinel-Shard-Id")
        if not (shard and meta["run_id"] and meta["kind"] and meta["media_type"]):
            raise BadRequest("an artifact PUT carries its kind, media type, run id and shard id headers (8.2)")
        with self._lock:
            index = self._index(shard)
            if index["failed"]:
                return respond(409, {"refused": f"{shard} is import_failed"})
            try:
                self._incoming(shard).put(
                    req.body, name=meta["name"], kind=meta["kind"], media_type=meta["media_type"],
                    redaction=Redaction(meta["redaction_version"]) if meta["redaction_version"] else None,
                    expected_sha256=sha, checkpoint_id=meta["checkpoint_id"], side=meta["side"])
            except ArtifactHashMismatch as e:
                n = index["mismatches"].get(sha, 0) + 1
                index["mismatches"][sha] = n
                if n <= MAX_REHASH:
                    self._save_index(shard, index)
                    return respond(422, {"error": "upload_hash_mismatch", "re_request": True, "attempt": n,
                                         "detail": str(e)})
                index["failed"] = True
                self._save_index(shard, index)
                unredacted = None
            except RedactionRefused as e:
                unredacted = str(e)
            except SecretLeaked as e:
                return respond(422, {"error": "secret_leaked", "refs": list(e.refs)})
            except ArtifactError as e:
                return respond(422, {"error": "artifact_refused", "detail": str(e)})
            else:
                index["uploads"][sha] = {k: v for k, v in meta.items() if k != "run_id"}
                self._save_index(shard, index)
                return self._maybe_import(shard) if index["ship"] is not None else respond(
                    200, {"sha256": sha, "stored": True})
        if unredacted is None:                         # the fourth mismatch: the shard fails (8.9)
            self.writer.submit(lambda s: fail_shard(s, meta["run_id"], shard, detail="upload_hash_mismatch"))
            return respond(422, {"error": "upload_hash_mismatch", "re_request": False, "shard": "import_failed"})
        if meta["checkpoint_id"]:
            self.writer.submit(lambda s: refuse_unredacted(s, meta["run_id"], meta["checkpoint_id"], shard))
        return respond(422, {"error": "redaction_version_missing", "detail": unredacted})

    def webhook(self, req: Request) -> Response:
        """The pipeline webhook into the run queue (5.1, 11.2)."""
        if self.queue is None:
            return respond(503, {"error": "no run queue in this service"})
        try:
            event = webhook_to_request(req.json() or {})
        except WebhookError as e:
            raise BadRequest(str(e)) from e
        return respond(200, {"outputs": [type(o).__name__ for o in self._queue(event)]})

    def _queue(self, event: Any) -> list[Any]:
        if self.queue is None:
            return []
        outputs = self.queue.handle(event)
        if self.on_queue is not None:
            self.on_queue(outputs)
        return outputs

    def action(self, req: Request) -> Response:
        """A report UI action (8.7, 9.6): authorized and written by `report.actions.apply` on the writer."""
        if self.actions is None:
            return respond(503, {"error": "actions are not configured in this service"})
        doc = req.json() or {}
        names = {f.name for f in dataclasses.fields(ActionRequest)}
        unknown = sorted(set(doc) - names)
        if unknown or not doc.get("login"):
            raise BadRequest(f"an action request names its login; unknown fields {unknown}")
        for k in ("change_authors",):
            if k in doc:
                doc[k] = tuple(doc[k])
        ar = ActionRequest(**doc)
        try:
            result = self.writer.submit(lambda s: apply_action(req.params["action"], s, self.actions, ar))
        except (StoreRefused, TaxonomyError) as e:
            return self._refusal(e)
        for sha_id, data in result.payloads.items():
            self.payload_sink(sha_id, data)
        return respond(200, result.to_dict())

    # -- the dead-man timer -------------------------------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> list[Fired]:
        """Close every run past its dead-man deadline (5.14); each through the writer."""
        now = now or self.clock()
        out = []
        for w in self.timer.due(now):
            try:
                out.append(self.writer.submit(lambda s, w=w: fire(s, self.timer, w, now)))
            except (StoreRefused, TaxonomyError) as e:
                w.fired = False                        # a refused close is retried at the next tick
                self.tick_errors.append((w.run_id, str(e)))
        return out


def fail_shard(store: Store, run_id: str, shard_id: str, *, detail: str) -> None:
    """8.9 upload hash mismatch after 3 re-requests: the shard `import_failed`, the run `aborted_harness (reason:
    artifact_persist_failed)` with every silent scoped checkpoint `harness_error` of the same reason."""
    store.update("shards", {"shard_id": shard_id}, {"state": "import_failed"})
    run = store.query("SELECT run_type, lifecycle FROM runs WHERE run_id = ?", (run_id,))
    if not run or run[0]["lifecycle"] == "finished":
        return
    store.fill_unreached(run_id, "harness_error", "artifact_persist_failed", detail=detail)
    store.end_run(RunOutcome(run_id, run[0]["run_type"], "aborted_harness", "reported",
                             reason="artifact_persist_failed"),
                  summary={"abort": {"detail": detail, "shard": shard_id}})


def refuse_unredacted(store: Store, run_id: str, checkpoint_id: str, shard_id: str | None) -> bool:
    """5.12, 8.9: an upload without a redaction version makes its checkpoint `harness_error (reason:
    redaction_version_missing)` while the run is open and the checkpoint is scoped and silent; True when written."""
    run = store.query("SELECT lifecycle FROM runs WHERE run_id = ?", (run_id,))
    scope = store.query("SELECT persona FROM run_scope WHERE run_id = ? AND checkpoint_id = ?", (run_id, checkpoint_id))
    if not run or run[0]["lifecycle"] == "finished" or not scope or store.get_outcome(run_id, checkpoint_id):
        return False
    c = store.query("SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,))[0]
    store.write_checkpoint(CheckpointOutcome(
        run_id=run_id, checkpoint_id=checkpoint_id, outcome="harness_error", reason="redaction_version_missing",
        workspace_id=c["workspace_id"], persona=scope[0]["persona"], data_mode=c["data_mode"], tier=c["tier"],
        flow_id=c["flow_id"], step_id=c["step_id"]), shard_id=shard_id)
    return True


def make_service(writer: Writer, importer: ShardImporter | None, queue: Queue | None, **kw: Any) -> ReporterService:
    """The Reporter service (module docstring); keyword arguments as `ReporterService`."""
    return ReporterService(writer, importer, queue, **kw)
