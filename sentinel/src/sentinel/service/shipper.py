"""The rig side of the hand-off (5.1, 5.12, 8.2): the client that ships rows and redacted artifacts to the Reporter
service, and the plain-HTTP transports it and the report UI use.

`Shipper(base_url, transport)` speaks the service's local API (`service.app`):
- `upload_scope(run_id, shard_id, entries)` on entry to `running`, so the control host can fill outcomes for a dead
  rig (5.12);
- `ship_checkpoint(run_id, shard_id, outcome, ...)` as each checkpoint completes: every artifact with
  `PUT /v1/artifacts/{sha256}` first, then the batch with `POST /v1/runs/{run_id}/results` (an outcome row never
  points at bytes the service has not verified, 8.2);
- `ship_shard(run_id, shard_id, objects, ...)` at `reported`: `POST /v1/shards/{shard_id}/ship` with the manifest,
  then a PUT for every object the service still needs (8.2 shard import);
- `run_completed`, `run_started` and `heartbeat`, the events the dead-man timer reads (5.14).
Every call is idempotent on the service, so a call that failed in transit (connection error or a 5xx answer) is
retried on the 9.1 schedule's first steps (1, 2, 5, 15 and 60 s here) and then raises `ShipError`; a 4xx answer is
final and raises at once, except the service's hash-mismatch re-request, which re-reads the object's bytes and
sends them again for as long as the service asks (it stops after 3, 8.9).

Transports: `UrllibTransport` (stdlib `urllib.request`, the production client) and `WsgiTransport` (calls a WSGI
application in process: tests, and a UI and service hosted in one process).

Choices where the spec is silent (reported):
- Artifact headers: `Content-Type` (media type), `X-Sentinel-Kind`, `X-Sentinel-Redaction-Version`,
  `X-Sentinel-Run-Id`, `X-Sentinel-Checkpoint-Id`, plus `X-Sentinel-Shard-Id`, `X-Sentinel-Name` and
  `X-Sentinel-Side`, which place the object in `incoming/<shard_id>/` and in the 8.4 manifest.
- The retry delays are seconds, not the 9.1 minutes: the rig keeps retrying until the run's deadline by calling
  again; the dead-man timer closes a run whose rig never gets through (5.12).
"""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import quote, unquote, urlsplit

RETRY_DELAYS_S = (1, 2, 5, 15, 60)       # the first steps of the 9.1 schedule, in seconds
TIMEOUT_S = 30
HEADER = {"kind": "X-Sentinel-Kind", "redaction_version": "X-Sentinel-Redaction-Version",
          "run_id": "X-Sentinel-Run-Id", "checkpoint_id": "X-Sentinel-Checkpoint-Id",
          "shard_id": "X-Sentinel-Shard-Id", "name": "X-Sentinel-Name", "side": "X-Sentinel-Side"}


class ShipError(Exception):
    """A call the service refused (4xx) or that did not get through after every retry."""

    def __init__(self, message: str, response: "Response | None" = None):
        super().__init__(message)
        self.response = response


@dataclass(frozen=True)
class Response:
    """One HTTP answer."""
    status: int
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


class Transport(Protocol):
    """Plain HTTP: one request, one answer. A connection failure raises `ConnectionError`."""

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                body: bytes = b"") -> Response:
        """Send the request and return the answer, whatever its status."""


class UrllibTransport:
    """The production transport: stdlib `urllib.request` with a timeout."""

    def __init__(self, timeout_s: float = TIMEOUT_S):
        self.timeout_s = timeout_s

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                body: bytes = b"") -> Response:
        req = urllib.request.Request(url, data=body if method in ("POST", "PUT") else None, method=method,
                                     headers=dict(headers or {}))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return Response(r.status, r.read(), dict(r.headers))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read(), dict(e.headers or {}))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ConnectionError(str(e)) from e


class WsgiTransport:
    """Calls a WSGI application in process; the URL's path and query are what the application sees."""

    def __init__(self, app: Callable[..., Iterable[bytes]]):
        self.app = app

    def request(self, method: str, url: str, *, headers: Mapping[str, str] | None = None,
                body: bytes = b"") -> Response:
        parts = urlsplit(url)
        environ: dict[str, Any] = {
            "REQUEST_METHOD": method, "PATH_INFO": unquote(parts.path) or "/", "QUERY_STRING": parts.query,
            "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body), "SERVER_NAME": "local",
            "SERVER_PORT": "80", "wsgi.url_scheme": "http"}
        for k, v in (headers or {}).items():
            key = k.upper().replace("-", "_")
            environ[key if key in ("CONTENT_TYPE", "CONTENT_LENGTH") else f"HTTP_{key}"] = v
        seen: dict[str, Any] = {}

        def start_response(status: str, hdrs: list[tuple[str, str]], exc_info: Any = None) -> None:
            seen["status"], seen["headers"] = int(status.split()[0]), dict(hdrs)

        out = b"".join(self.app(environ, start_response))
        return Response(seen["status"], out, seen["headers"])


@dataclass(frozen=True)
class Upload:
    """One redacted object to ship: its hash, 8.3 kind, media type and how to read its bytes from the rig's set."""
    sha256: str
    kind: str
    media_type: str
    read: Callable[[], bytes]
    name: str | None = None
    checkpoint_id: str | None = None
    side: str | None = None
    redaction_version: str | None = None

    def manifest_entry(self) -> dict:
        return {"sha256": self.sha256, "kind": self.kind, "media_type": self.media_type,
                "name": self.name or self.sha256, "checkpoint_id": self.checkpoint_id, "side": self.side,
                "redaction_version": self.redaction_version}


def _jsonable(obj: Any) -> Any:
    to_dict = getattr(obj, "to_dict", None)
    return to_dict() if callable(to_dict) else obj


class Shipper:
    """The rig's client of the Reporter service's local API (module docstring)."""

    def __init__(self, base_url: str, transport: Transport, *, delays: Sequence[float] = RETRY_DELAYS_S,
                 sleep: Callable[[float], None] = time.sleep):
        self.base = base_url.rstrip("/")
        self.transport = transport
        self.delays = tuple(delays)
        self.sleep = sleep

    def _call(self, method: str, path: str, *, body: bytes = b"", headers: Mapping[str, str] | None = None,
              ok: tuple[int, ...] = (200, 201, 202, 204)) -> Response:
        """One idempotent call with the retries of the module docstring."""
        last: str = ""
        for attempt in range(len(self.delays) + 1):
            if attempt:
                self.sleep(self.delays[attempt - 1])
            try:
                r = self.transport.request(method, self.base + path, headers=headers, body=body)
            except ConnectionError as e:
                last = f"connection failed: {e}"
                continue
            if r.status >= 500:
                last = f"{r.status} {r.body[:200]!r}"
                continue
            if r.status not in ok:
                raise ShipError(f"{method} {path}: {r.status} {r.body[:300]!r}", r)
            return r
        raise ShipError(f"{method} {path}: gave up after {len(self.delays) + 1} attempts ({last})")

    def _json(self, method: str, path: str, doc: Any) -> Response:
        return self._call(method, path, body=json.dumps(doc, sort_keys=True).encode("utf-8"),
                          headers={"Content-Type": "application/json"})

    def put_artifact(self, run_id: str, shard_id: str, up: Upload) -> Response:
        """PUT one object; on the service's hash-mismatch re-request, read the bytes again and resend (8.9)."""
        headers = {"Content-Type": up.media_type}
        meta = {"kind": up.kind, "redaction_version": up.redaction_version, "run_id": run_id,
                "checkpoint_id": up.checkpoint_id, "shard_id": shard_id, "name": up.name, "side": up.side}
        headers.update({HEADER[k]: v for k, v in meta.items() if v})
        while True:
            try:
                return self._call("PUT", f"/v1/artifacts/{up.sha256}", body=up.read(), headers=headers)
            except ShipError as e:
                doc = e.response.json() if e.response is not None and e.response.body else None
                if not (isinstance(doc, dict) and doc.get("re_request")):
                    raise

    def upload_scope(self, run_id: str, shard_id: str, entries: Iterable[tuple[str, str]], *,
                     at: str | None = None) -> Response:
        """The scope manifest at entry to `running` (5.12): (checkpoint id, persona) per scoped checkpoint."""
        doc = {"shard_id": shard_id, "checkpoints": [{"checkpoint_id": c, "persona": p} for c, p in entries],
               "at": at}
        return self._json("POST", f"/v1/runs/{quote(run_id, safe='')}/scope", doc)

    def ship_checkpoint(self, run_id: str, shard_id: str, outcome: Any, *, steps: Sequence[Any] = (),
                        findings: Sequence[Any] = (), artifacts: Sequence[Upload] = (), side: str | None = None
                        ) -> Response:
        """One checkpoint's batch: its artifacts, then its rows (5.1, 8.2)."""
        for up in artifacts:
            self.put_artifact(run_id, shard_id, up)
        doc = {"shard_id": shard_id, "side": side, "outcome": _jsonable(outcome),
               "steps": [_jsonable(s) for s in steps], "findings": [_jsonable(f) for f in findings],
               "artifacts": [a.sha256 for a in artifacts]}
        return self._json("POST", f"/v1/runs/{quote(run_id, safe='')}/results", doc)

    def ship_shard(self, run_id: str, shard_id: str, objects: Sequence[Upload], *,
                   run_store_sha256: str | None = None, ledger_sha256: str | None = None,
                   ledger_rows: int | None = None) -> Response:
        """The shard hand-off at `reported` (8.2): the manifest, then every object the service still needs. The
        last answer says whether the shard was imported."""
        doc = {"run_id": run_id, "objects": [o.manifest_entry() for o in objects],
               "run_store_sha256": run_store_sha256, "ledger_sha256": ledger_sha256, "ledger_rows": ledger_rows}
        r = self._json("POST", f"/v1/shards/{quote(shard_id, safe='')}/ship", doc)
        need = set((r.json() or {}).get("need") or ())
        for o in objects:
            if o.sha256 in need:
                r = self.put_artifact(run_id, shard_id, o)
        return r

    def run_started(self, run_id: str, run_type: str, deadline: str, shards: Mapping[str, str]) -> Response:
        """The run-started event: run type, run deadline and the rig of each shard (5.13, 5.14)."""
        return self._json("POST", f"/v1/runs/{quote(run_id, safe='')}/started",
                          {"run_type": run_type, "deadline": deadline, "shards": dict(shards)})

    def run_completed(self, run_id: str, status: str, *, canaries_passed: bool = False) -> Response:
        """The run-completed event the dead-man timer waits for (5.14)."""
        return self._json("POST", f"/v1/runs/{quote(run_id, safe='')}/completed",
                          {"status": status, "canaries_passed": canaries_passed})

    def heartbeat(self, rig_id: str, at: str) -> Response:
        """The rig heartbeat summary every 30 s (5.1)."""
        return self._json("POST", f"/v1/rigs/{quote(rig_id, safe='')}/heartbeat", {"at": at})
