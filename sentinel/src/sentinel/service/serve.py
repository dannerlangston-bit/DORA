"""`sentinel serve`: the control host's Reporter service, report UI and timed work in one process (5.1, 8.2, 8.7,
9.1, 9.5).

`compose(db, artifact_root, config_dir, ...)` builds, in order:
1. the Central Store behind its single writer thread (`store.writer.WriterService`, the store opened with the
   ops/oncall.yaml roster and system owners and the decisions ledger, 8.2, 8.7, 9.7);
2. the action context of the report UI's actions (`report.actions.ActionContext`: the role directory, the
   sentinel-config host, the Region Registry and redaction list of the config, the week's triage rotation member);
3. the control host (`service.host.ControlHost`: the outbox, the overdue statuses, the undelivered-page watch, the
   SLA escalation, the digests, the reminders, the Jira poll, the backup and the audit) over the configured
   transports (`report.senders.Transports`);
4. the Reporter service (`service.app.make_service`) with the store importer (`store.importer.import_shard`), its
   action hook (`ControlHost.after_action`) and its maintenance jobs, and the dead-man timer restored from the
   store (`DeadmanTimer.restore`);
5. the report UI (`ui.app.make_app`) over a read-only pool, calling the service's local API in process
   (`service.shipper.WsgiTransport`; the API is the control host's own, 8.7).

`serve(composed, host=, port=, ui_port=, tick_s=, once=)` binds both WSGI applications on localhost
(`wsgiref`, one thread each) and ticks the dead-man timer and the control host every `tick_s` seconds until
interrupted, or once with `once`.

Choices where the spec is silent (reported):
1. The integrations: the live GitHub Enterprise, Jira, Slack and paging transports are Phase 2 items (11.1), so
   `integrations="none"` leaves every job queued (the run list shows them) and `"simulated"` posts to the in-memory
   transports, with the PR host kept in `state_dir/pr-host.json` (`prhost.FilePrHost`).
2. The report UI's action context reads the Region Registry and the redaction list of the config checkout given,
   for every run (the run's own config commit is not checked out on the control host); the triage rotation member
   is the one on duty when the service starts.
3. The service has no run queue here (`/v1/webhook` answers 503): the run queue belongs to the orchestration side.
4. Payloads are read from the outbox set (`<artifact root>/outbox`, where new payloads go) and, for jobs other
   writers queued with their payload in a run's set (the Nightly's quarantine PR, the reopen jobs of fix
   verification), from every `runs/<run id>` set (`CentralPayloads`).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from ..lint.repo import Repo
from ..pipeline.config import SentinelConfig
from ..report import jira, outbox
from ..report.actions import ActionContext, RoleDirectory
from ..report.alerting import MemoryMailer, MemoryPager
from ..report.prhost import FilePrHost
from ..report.reporting_config import ReportingConfig
from ..report.senders import MemoryConfigRepo, MemorySlack, Transports
from ..sla import OnCall
from ..store import importer as store_importer
from ..store.artifacts import ArtifactMissing, ArtifactStore
from ..store.store import Store
from ..store.writer import WriterService
from ..ui.app import HttpServiceClient, ReaderPool, ReportUI, header_identity, make_app
from ..egress import EgressAllowlist
from .app import ReporterService, make_service
from .host import ControlHost, TickReport, WriterAdapter
from .shipper import WsgiTransport

INTEGRATIONS = ("none", "simulated")
LOCAL_API = "http://reporter.local"            # the in-process service URL the report UI calls


class CentralPayloads:
    """Outbox payloads on the control host (choice 4): written to `<root>/outbox`, read from it or any run set."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.outbox = ArtifactStore.at(self.root, "outbox")

    def put(self, data: bytes, *, name: str, kind: str, media_type: str) -> Any:
        return self.outbox.put(data, name=name, kind=kind, media_type=media_type)

    def get(self, sha: str) -> bytes:
        try:
            return self.outbox.get(sha)
        except ArtifactMissing:
            pass
        for d in sorted((self.root / "runs").glob("*")) if (self.root / "runs").is_dir() else ():
            try:
                return ArtifactStore.at(self.root, f"runs/{d.name}").get(sha)
            except ArtifactMissing:
                continue
        raise ArtifactMissing(sha)


class StoreImporter:
    """The service's shard importer over `store.importer.import_shard` (8.2)."""

    def __init__(self, artifact_root: str | Path):
        self.artifact_root = Path(artifact_root)

    def import_shard(self, store: Store, incoming_dir: Path, run_id: str, shard_id: str) -> Any:
        return store_importer.import_shard(store, incoming_dir, run_id, artifact_root=self.artifact_root)


def transports_for(integrations: str, state_dir: str | Path) -> Transports:
    """The transports of `integrations` (choice 1)."""
    if integrations == "none":
        return Transports()
    if integrations == "simulated":
        return Transports(pr_host=FilePrHost(Path(state_dir) / "pr-host.json"), jira=jira.MemoryJira(),
                          slack=MemorySlack(), pager=MemoryPager(), mailer=MemoryMailer(),
                          config_repo=MemoryConfigRepo())
    raise ValueError(f"integrations {integrations!r}; one of {INTEGRATIONS}")


@dataclass
class Composed:
    """What `compose` built (module docstring)."""
    writer_service: WriterService
    writer: WriterAdapter
    service: ReporterService
    ui: ReportUI
    host: ControlHost
    transports: Transports
    config: SentinelConfig

    def tick(self, now: datetime) -> TickReport:
        """One timer tick: the dead-man deadlines, then the control host's work."""
        self.service.tick(now)
        return self.host.tick(now)

    def close(self) -> None:
        self.host.join(timeout=60)
        self.writer_service.close()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compose(db: str | Path, artifact_root: str | Path, config_dir: str | Path, *, backup_root: str | Path | None = None,
            integrations: str = "none", state_dir: str | Path | None = None,
            base_url: str = "https://sentinel.internal", clock: Callable[[], datetime] = _utc_now, background: bool = True) -> Composed:
    """Build the control host's process (module docstring)."""
    cfg = SentinelConfig.load(config_dir)
    repo = Repo.load(config_dir)
    oncall_doc = (repo.one("oncall").data if repo.one("oncall") is not None else None) or {}
    directory = RoleDirectory.from_oncall(oncall_doc)
    ws = WriterService(lambda: Store(db, roster=directory.roster, system_owners=directory.system_owners,
                                     decisions_ledger=True)).start()
    writer = WriterAdapter(ws)
    transports = transports_for(integrations, state_dir or Path(db).parent)
    redaction = repo.one("redaction")
    reporting: ReportingConfig | None = cfg.reporting
    ctx = ActionContext(directory, repo=transports.config_repo, registry=lambda _run_id: cfg.regions,
                        redaction=redaction.data if redaction is not None else None,
                        rotation=OnCall.from_doc(oncall_doc).rotation_member("triage", clock().date()),
                        clock=lambda: clock().isoformat(timespec="seconds"))
    allow_path = Path(config_dir) / "policy" / "egress_allowlist.yaml"
    allowlist = EgressAllowlist.load(allow_path) if allow_path.is_file() else EgressAllowlist.empty()
    host = ControlHost(writer, CentralPayloads(artifact_root), allowlist=allowlist, transports=transports,
                       reporting=reporting, oncall=oncall_doc, base_url=base_url, artifact_root=artifact_root,
                       backup_root=backup_root, background=background)
    jobs: dict[str, Callable[[], Any]] = {}
    if backup_root is not None:
        jobs = {"backup": lambda: _summary(host.backup(clock())), "audit": lambda: _summary(host.audit(clock()))}
    service = make_service(writer, StoreImporter(artifact_root), None, artifact_root=artifact_root, actions=ctx,
                           clock=clock, on_action=lambda res, req: host.after_action(res, req, clock()), jobs=jobs)
    writer.submit(service.timer.restore)
    ui = make_app(ReaderPool(db), header_identity(), service=HttpServiceClient(LOCAL_API, WsgiTransport(service)),
                  artifact_roots=[artifact_root], clock=clock)
    return Composed(ws, writer, service, ui, host, transports, cfg)


def _summary(result: Any) -> dict[str, Any]:
    return {k: v for k, v in vars(result).items() if isinstance(v, (str, int, float, bool, type(None)))}


class _Quiet(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:     # noqa: A002 - the stdlib's signature
        return


def bind(app: Callable[..., Any], host: str, port: int) -> WSGIServer:
    """A `wsgiref` server for `app` on `host:port` (0: any free port)."""
    return make_server(host, port, app, handler_class=_Quiet)


def serve(c: Composed, *, host: str = "127.0.0.1", port: int = 8780, ui_port: int = 8781, tick_s: float = 60.0,
          once: bool = False, clock: Callable[[], datetime] = _utc_now,
          out: Callable[[str], None] = print) -> int:
    """Serve the service and the UI on `host` and tick every `tick_s` seconds (module docstring). Returns 0, or 3
    when the control host asked to stop (a failed weekly integrity check, 8.9)."""
    servers = [bind(c.service, host, port), bind(c.ui, host, ui_port)]
    threads = [threading.Thread(target=s.serve_forever, name=f"sentinel-http-{i}", daemon=True)
               for i, s in enumerate(servers)]
    for t in threads:
        t.start()
    out(f"Reporter service on http://{host}:{servers[0].server_port}, report UI on "
        f"http://{host}:{servers[1].server_port}")
    stop = threading.Event()
    code = 0
    try:
        while True:
            rep = c.tick(clock())
            out(f"tick: {', '.join(rep.ran)}; outbox {len(rep.dispatch.attempts) if rep.dispatch else 0} attempt(s), "
                f"{len(rep.dispatch.waiting) if rep.dispatch else 0} waiting")
            if c.host.stop_requested:
                out(f"stopping: {c.host.stop_requested}")
                code = 3
                break
            if once or stop.wait(tick_s):
                break
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
        c.close()
    return code


__all__ = ["CentralPayloads", "Composed", "INTEGRATIONS", "StoreImporter", "bind", "compose", "serve",
           "transports_for"]
