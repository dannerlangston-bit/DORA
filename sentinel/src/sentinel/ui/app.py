"""The report UI application (9.5, 8.7, 9.1): read-only pages over the Central Store, actions through the Reporter
service.

`make_app(store_reader, identity, ...)` returns a `ReportUI`, a WSGI application serving the routes
`report.inline.Urls` composes (`/runs/<run>`, `/runs/<run>/coverage`, `/cp/<ref>`, `/f/<finding>`,
`/act/<action>?f=<ref>`, `/a/<sha256>`, `/docs/manifest-template`) and the other 9.5 pages (`/runs`, `/triage`,
`/decisions`, `/baselines`, `/coverage`, `/canary`, `/metrics`, `/runs/compare`).

- `store_reader` is a `ReaderPool` (or the Central Store path, for one): read-only connections opened by
  `store.connect_reader`, one short read transaction per request (8.7).
- `identity(environ)` is the SSO identity of the request (a login, or None: 401). `header_identity` reads the header
  the SSO proxy sets; tests pass the header themselves.
- `service` is the Reporter service client (`ServiceClient`: `health()` and `act(action, fields)`), the only write
  path (8.2, 9.1). When it is absent or its health check fails, every page shows the read-only banner, the action
  buttons are disabled and an action POST is refused with 503, never queued (8.2, 9.1).
- `artifact_roots` are the artifact tree roots `/a/<sha256>` reads (`D:\\sentinel\\artifacts\\` on the control host).

Choices (reported): the UI and the service are plain WSGI (no new dependency; see `service.app`); the reader pool keeps
one connection per serving thread, since a SQLite connection belongs to the thread that opened it; the login
posted with an action is the request's SSO identity, never a form field.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from ..service.app import Request, Response, Router, respond
from ..service.shipper import Transport, UrllibTransport
from ..store.store import connect_reader
from . import pages
from .pages import NotFound, Page, e

READ_ONLY_BANNER = ("The Reporter service is unreachable: the report is read-only and actions are refused, not "
                    "queued (8.2).")
CSS = ("body{font:14px/1.45 system-ui,sans-serif;margin:16px;color:#1b1f24;background:#fff}"
       "table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #d0d7de;padding:3px 6px;text-align:left;"
       "vertical-align:top}.banner{background:#fff3cd;border:1px solid #d4a72c;padding:8px}.warn{color:#9a6700}"
       ".red{color:#cf222e}.muted,.disabled{color:#6e7781}img.px{image-rendering:pixelated;max-width:none}"
       ".flicker{position:relative;display:inline-block}.flicker img{display:block}"
       ".flicker img+img{position:absolute;left:0;top:0;animation:flick calc(2*var(--flicker-ms)) steps(1) infinite}"
       "@keyframes flick{0%{opacity:0}50%{opacity:1}}.box{position:absolute;border:1px solid #cf222e}"
       ".box.region{border-color:#0969da}dl{display:grid;grid-template-columns:max-content auto;gap:2px 12px}"
       "dt{color:#57606a}dd{margin:0}.prov{font-size:12px;color:#57606a}")


class ServiceClient(Protocol):
    """The report UI's view of the Reporter service's local API."""

    def health(self) -> bool:
        """True when the service answers its health check."""

    def act(self, action: str, fields: Mapping[str, Any]) -> tuple[int, dict]:
        """POST one action with the SSO login in `fields`; returns the status and the JSON answer."""


class HttpServiceClient:
    """`ServiceClient` over HTTP to the service's local API (8.2), through any `Transport`."""

    def __init__(self, base_url: str, transport: Transport | None = None):
        self.base = base_url.rstrip("/")
        self.transport = transport or UrllibTransport(timeout_s=5)

    def health(self) -> bool:
        try:
            return self.transport.request("GET", f"{self.base}/v1/health").status == 200
        except ConnectionError:
            return False

    def act(self, action: str, fields: Mapping[str, Any]) -> tuple[int, dict]:
        try:
            r = self.transport.request("POST", f"{self.base}/v1/actions/{action}",
                                       headers={"Content-Type": "application/json"},
                                       body=json.dumps(dict(fields), sort_keys=True).encode("utf-8"))
        except ConnectionError as ex:
            return 503, {"error": f"the Reporter service is unreachable: {ex}"}
        return r.status, (r.json() or {})


class ReaderPool:
    """Read-only connections to the Central Store, one per serving thread, one short transaction per request (8.7)."""

    def __init__(self, path: str | Path, *, factory: Callable[[str | Path], sqlite3.Connection] = connect_reader):
        self.path = path
        self.factory = factory
        self._local = threading.local()

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self.factory(self.path)
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection()
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.execute("COMMIT")


def header_identity(header: str = "X-Forwarded-User") -> Callable[[Mapping[str, Any]], str | None]:
    """The SSO identity from the header the authenticating proxy sets (9.1, 8.7)."""
    key = "HTTP_" + header.upper().replace("-", "_")

    def identity(environ: Mapping[str, Any]) -> str | None:
        v = environ.get(key)
        return str(v).strip() or None if v else None
    return identity


@dataclass
class ReportUI:
    """The WSGI application of the report UI (module docstring)."""
    reader: ReaderPool
    identity: Callable[[Mapping[str, Any]], str | None]
    service: ServiceClient | None = None
    artifact_roots: Sequence[Path] = ()
    clock: Callable[[], datetime] = pages.utc_now
    sla: Callable[[str, str, int | None], str | None] | None = None

    def __post_init__(self) -> None:
        r = self.router = Router()
        r.add("GET", "/", lambda req: self._page(req, lambda c, w: pages.run_list(c, req.query)))
        r.add("GET", "/runs", lambda req: self._page(req, lambda c, w: pages.run_list(c, req.query)))
        r.add("GET", "/runs/compare", lambda req: self._page(req, lambda c, w: pages.compare_runs(
            c, req.query.get("a", ""), req.query.get("b", ""))))
        r.add("GET", "/runs/{run_id}", lambda req: self._page(req, lambda c, w: pages.run_detail(
            c, req.params["run_id"], req.query, writable=w)))
        r.add("GET", "/runs/{run_id}/coverage", lambda req: self._page(req, lambda c, w: pages.coverage(
            c, req.params["run_id"], req.query)))
        r.add("GET", "/coverage", lambda req: self._page(req, lambda c, w: pages.coverage(c, None, req.query)))
        r.add("GET", "/cp/{ref:path}", lambda req: self._page(req, lambda c, w: pages.checkpoint_detail(
            c, req.params["ref"], req.query, writable=w)))
        r.add("GET", "/f/{finding:path}", lambda req: self._page(req, lambda c, w: pages.finding_page(
            c, req.params["finding"], writable=w)))
        r.add("GET", "/triage", lambda req: self._page(req, lambda c, w: pages.triage_queue(
            c, req.query, writable=w, sla=self.sla)))
        r.add("GET", "/decisions", lambda req: self._page(req, lambda c, w: pages.decisions_page(
            c, req.query, writable=w, now=self.clock())))
        r.add("GET", "/baselines", lambda req: self._page(req, lambda c, w: pages.baseline_queue(
            c, req.query, writable=w)))
        r.add("GET", "/canary", lambda req: self._page(req, lambda c, w: pages.canary_panel(c, req.query)))
        r.add("GET", "/metrics", self.metrics)
        r.add("GET", "/docs/manifest-template", lambda req: self._page(req, lambda c, w: pages.manifest_template()))
        r.add("GET", "/act/{action}", lambda req: self._page(req, lambda c, w: pages.action_form(
            c, req.params["action"], req.query, writable=w)))
        r.add("POST", "/act/{action}", self.act)
        r.add("GET", "/a/{sha256}", self.artifact)

    def __call__(self, environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        if self.identity(environ) is None:
            status, headers, body = respond(401, self._layout(Page("Sign in", "<h1>Sign in required</h1><p>The "
                                                                   "report UI is behind SSO.</p>", status=401), True),
                                            content_type="text/html; charset=utf-8")
            start_response("401 Unauthorized", headers)
            return [body]
        return self.router.wsgi(environ, start_response)

    def writable(self) -> bool:
        if self.service is None:
            return False
        try:
            return bool(self.service.health())
        except ConnectionError:
            return False

    def _layout(self, page: Page, writable: bool) -> str:
        nav = " | ".join(f'<a href="{h}">{e(t)}</a>' for h, t in (
            ("/runs", "Runs"), ("/triage", "Triage queue"), ("/decisions", "Decisions"),
            ("/baselines", "Baselines"), ("/coverage", "Coverage"), ("/canary", "Canary"), ("/metrics", "Metrics")))
        banner = "" if writable else f'<p class="banner" role="alert">{e(READ_ONLY_BANNER)}</p>'
        prov = page.provenance or {"provenance": "no run on this page"}
        prov_html = '<dl class="prov">' + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in prov.items()) + "</dl>"
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
                f'content="width=device-width, initial-scale=1"><title>{e(page.title)} - Sentinel</title>'
                f"<style>{CSS}</style></head><body>{banner}<nav>{nav}</nav>"
                f'<section class="provenance">{prov_html}</section><main>{page.body}</main></body></html>')

    def _html(self, page: Page, writable: bool) -> Response:
        return respond(page.status, self._layout(page, writable), content_type="text/html; charset=utf-8")

    def _page(self, req: Request, render: Callable[[sqlite3.Connection, bool], Page]) -> Response:
        writable = self.writable()
        try:
            with self.reader.read() as conn:
                page = render(conn, writable)
        except NotFound as ex:
            page = Page("Not found", f"<h1>Not found</h1><p>{e(ex)}</p>", status=404)
        return self._html(page, writable)

    def metrics(self, req: Request) -> Response:
        if req.query.get("format") == "csv":
            with self.reader.read() as conn:
                text = pages.metrics_csv(conn, req.query, self.clock())
            return respond(200, text, content_type="text/csv; charset=utf-8",
                           headers=[("Content-Disposition", 'attachment; filename="sentinel-metrics.csv"')])
        return self._page(req, lambda c, w: pages.metrics(c, req.query, self.clock()))

    def act(self, req: Request) -> Response:
        """Post the action to the Reporter service with the SSO identity; refused with the banner when it is down."""
        action = req.params["action"]
        writable = self.writable()
        if not writable:
            return self._html(Page(f"{action}: refused", f'<h1>{e(action)}: refused</h1><p class="warn">'
                                                         f"{e(READ_ONLY_BANNER)}</p>", status=503), False)
        form = req.form()
        fields: dict[str, Any] = {"login": self.identity(req.environ)}
        rename = {"f": "finding"}
        for k, v in form.items():
            if v == "" or k == "login":
                continue
            key = rename.get(k, k)
            if key == "cluster":
                fields[key] = True
                continue
            fields[key] = int(v) if key in ("proposal", "job", "pr", "baseline_id") and v.isdigit() else v
        status, doc = self.service.act(action, fields)
        return self._html(pages.result_page(action, status, doc), writable)

    def artifact(self, req: Request) -> Response:
        with self.reader.read() as conn:
            try:
                out = pages.artifact(conn, req.params["sha256"], [Path(r) for r in self.artifact_roots])
            except NotFound as ex:
                out = Page("Not found", f"<h1>Not found</h1><p>{e(ex)}</p>", status=404)
        if isinstance(out, Page):
            return self._html(out, self.writable())
        return out


def make_app(store_reader: ReaderPool | str | Path, identity: Callable[[Mapping[str, Any]], str | None], *,
             service: ServiceClient | None = None, artifact_roots: Sequence[str | Path] = (),
             clock: Callable[[], datetime] = pages.utc_now,
             sla: Callable[[str, str, int | None], str | None] | None = None) -> ReportUI:
    """The report UI (module docstring). `store_reader` is a `ReaderPool` or the Central Store's path."""
    pool = store_reader if isinstance(store_reader, ReaderPool) else ReaderPool(store_reader)
    return ReportUI(pool, identity, service, tuple(Path(r) for r in artifact_roots), clock, sla)
