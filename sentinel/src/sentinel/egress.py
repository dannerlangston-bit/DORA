"""The harness egress policy (6.10 Egress, 6.10 Nightly reconciliation, 8.3 `egress_log`).

- `EgressAllowlist`: the signed `policy/egress_allowlist.yaml` (4.16): exact host names, each with a port and the
  purposes it may be reached for, signed by the security contact. `allowlist_problems` is the one check of its
  content that both the loader and the lint (L-XREF-19, `lint/checks/egress.py`) use.
- `EgressGate(allowlist, store)`: every outbound request of the harness goes through `request`. A host, port or
  purpose the allowlist does not name, an image without a redaction version (8.9) or an image bound for the PR
  host or Slack (6.10: only text and links go there) is refused before connecting: a `refused` row with its
  reason is written to `egress_log`, a `harness_error` finding (S3) is raised on the run, and `EgressRefused` is
  raised. Otherwise the request is sent and one `sent` row (with the HTTP status and request id) or `failed` row
  (no response) is written. `context(...)` sets row fields (checkpoint, artifact hash, redaction version, model
  id, purpose) for the requests made inside it, so a transport that cannot see them still logs them.
- `reconcile(rows, allowlist, firewall_log)` is the pure Nightly comparison: a `sent` row to a host outside the
  allowlist, or a firewall-permitted connection from a harness process with no matching row, is S1 and re-marks
  the runs concerned `aborted_harness (reason: egress_unlogged)`; a firewall block of a harness process is a
  `harness_error` finding (S3). The firewall log is an input (the rig firewall and its collector are Windows).

Choices where the spec is ambiguous (reported):

1. The signature is checked for presence (signed_by, signed_at, signature), not verified cryptographically: 4.16
   names no signature scheme and the harness carries no key.
2. A host is an exact DNS name: letters, digits and hyphens in dot-separated labels, no scheme, port, path,
   wildcard or other pattern character. Names compare case-blind; a host and port listed twice is a problem.
3. A request with an HTTP response is `sent` with that status, whatever it is; `failed` is a request that got no
   response (connection error, timeout). A refusal writes its finding once per gate for each host, port and
   purpose, so a retried request does not repeat the finding.
4. Reconciliation matches a firewall-permitted harness connection to a `sent` or `failed` row of the same host
   (and run, when both name one) within `window_s` seconds (default 60; the rig clocks are within 2 s, R7). A
   firewall entry without a run id raises the S1 finding but re-marks no run.
5. A config without `policy/egress_allowlist.yaml` (which L-XREF-19 accepts) has the empty allowlist
   (`EgressAllowlist.empty`): the harness HTTP client refuses every host, as the lint's choice 1 assumes, and the
   Nightly reconciliation compares the day's rows with it, so any `sent` row is to a host outside the allowlist.
6. A live client built without a run's gate (`reviewer.AnthropicTransport.from_env` with no `gate`) is not left
   ungated: it gets `refuse_all()`, a gate over the empty allowlist whose rows and findings go to the log
   (`LogSink`), since there is no run or store to write them to. Every request is refused before connecting.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence, TypeVar

import yaml

from .orchestrator import alerts
from .orchestrator.results import ControlResult, FindingSpec, RunVerdict
from .records import EGRESS_PURPOSES, EgressRow, Finding, now

T = TypeVar("T")
HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
MODEL_PURPOSES = frozenset({"review", "batch_review", "agent_step", "explorer_step", "model_check"})
TEXT_ONLY_PURPOSES = frozenset({"pr_comment", "slack"})          # 6.10, 8.7: images never go to the PR host or Slack
ROW_FIELDS = ("purpose", "checkpoint_id", "artifact_sha256", "redaction_version", "model_id", "image")
_CONTEXT: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar("sentinel_egress_context", default={})
log = logging.getLogger(__name__)


class AllowlistError(ValueError):
    """`policy/egress_allowlist.yaml` breaks 4.16 or L-XREF-19."""


class EgressRefused(RuntimeError):
    """A request the allowlist does not permit, refused before connecting (6.10); its row and finding exist."""

    def __init__(self, host: str, purpose: str, reason: str):
        super().__init__(f"egress to {host} for {purpose} refused: {reason}")
        self.host, self.purpose, self.reason = host, purpose, reason


def normalize_host(host: str) -> str:
    """Lower case, without a trailing dot."""
    return host.strip().lower().rstrip(".")


def allowlist_problems(data: Any) -> list[tuple[str, str]]:
    """(path, message) for every L-XREF-19 content problem of an allowlist document: exact hosts, known purposes,
    no host and port twice, the three signature fields present (choices 1, 2)."""
    out: list[tuple[str, str]] = []
    if not isinstance(data, Mapping):
        return [("", "the allowlist is a mapping (4.16)")]
    hosts = data.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        out.append(("hosts", "the allowlist names at least one host"))
        hosts = []
    seen: set[tuple[str, int]] = set()
    for i, h in enumerate(hosts):
        where = f"hosts[{i}]"
        if not isinstance(h, Mapping):
            out.append((where, "a host entry is a mapping"))
            continue
        host = h.get("host")
        if not isinstance(host, str) or not HOST_RE.match(normalize_host(host)):
            out.append((f"{where}.host", f"{host!r} is not an exact host name (no scheme, port, path, wildcard or "
                                         "pattern)"))
        port = h.get("port", 443)
        if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
            out.append((f"{where}.port", f"port {port!r} is not a TCP port"))
        elif isinstance(host, str):
            key = (normalize_host(host), port)
            if key in seen:
                out.append((f"{where}.host", f"{host}:{port} is listed twice"))
            seen.add(key)
        purposes = h.get("purposes")
        if not isinstance(purposes, list) or not purposes:
            out.append((f"{where}.purposes", "a host lists the purposes it may be reached for"))
        else:
            out += [(f"{where}.purposes", f"purpose {p!r} is not one of the egress_log purposes of 6.10")
                    for p in purposes if p not in EGRESS_PURPOSES]
    for k in ("signed_by", "signed_at", "signature"):
        if not isinstance(data.get(k), str) or not data[k].strip():
            out.append((k, f"the allowlist carries {k}: it is signed by the security contact (6.10)"))
    return out


@dataclass(frozen=True)
class AllowedHost:
    """One allowlist entry."""
    host: str
    purposes: frozenset[str]
    port: int = 443


@dataclass(frozen=True)
class EgressAllowlist:
    """The signed allowlist (4.16); construct with `from_dict` or `load`."""
    hosts: tuple[AllowedHost, ...]
    signed_by: str
    signed_at: str
    signature: str

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "EgressAllowlist":
        problems = allowlist_problems(data)
        if problems:
            raise AllowlistError("; ".join(f"{p}: {m}" if p else m for p, m in problems))
        return EgressAllowlist(
            tuple(AllowedHost(normalize_host(h["host"]), frozenset(h["purposes"]), int(h.get("port", 443)))
                  for h in data["hosts"]),
            str(data["signed_by"]), str(data["signed_at"]), str(data["signature"]))

    @staticmethod
    def load(path: str | Path) -> "EgressAllowlist":
        return EgressAllowlist.from_dict(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})

    @staticmethod
    def empty() -> "EgressAllowlist":
        """No host at all: the allowlist of a config without `policy/egress_allowlist.yaml` (choice 5)."""
        return EgressAllowlist((), "", "", "")

    def entry(self, host: str, port: int = 443) -> AllowedHost | None:
        h = normalize_host(host)
        return next((e for e in self.hosts if e.host == h and e.port == port), None)

    def allows_host(self, host: str, port: int | None = None) -> bool:
        """The host (on `port`, or on any listed port) is on the allowlist."""
        h = normalize_host(host)
        return any(e.host == h and (port is None or e.port == port) for e in self.hosts)

    def refusal(self, host: str, purpose: str, port: int = 443) -> str | None:
        """Why a request would be refused, or None when the allowlist permits it (6.10)."""
        if not self.hosts:
            return f"destination not allowlisted: {normalize_host(host)} (no signed allowlist: every host is refused)"
        if not self.allows_host(host):
            return f"destination not allowlisted: {normalize_host(host)}"
        e = self.entry(host, port)
        if e is None:
            return f"port {port} not allowlisted for {normalize_host(host)}"
        if purpose not in e.purposes:
            return f"purpose {purpose} not allowlisted for {e.host}"
        return None


class EgressSink(Protocol):
    """Where rows and findings go: `store.Store` has both methods."""

    def log_egress(self, row: EgressRow) -> int: ...

    def write_finding(self, finding: Finding, *, synthetic: bool = False) -> None: ...


class LogSink:
    """An `EgressSink` with no store, for a gate that belongs to no run (choice 6): each row and finding is logged
    (ERROR for a refusal, INFO otherwise) and nothing is written."""

    def __init__(self) -> None:
        self.rows: list[EgressRow] = []

    def log_egress(self, row: EgressRow) -> int:
        self.rows.append(row)
        log.log(logging.ERROR if row.status == "refused" else logging.INFO, "egress %s to %s for %s%s", row.status,
                row.destination, row.purpose, f": {row.refusal_reason}" if row.refusal_reason else "")
        return len(self.rows)

    def write_finding(self, finding: Finding, *, synthetic: bool = False) -> None:
        log.error("egress finding %s: %s", finding.kind, finding.description)


class EgressGate:
    """The harness HTTP client's policy point (module docstring). `run_id` and `shard_id` go on every row; a
    refusal's `harness_error` finding is written to the store when the gate has a run, and kept in `findings`."""

    def __init__(self, allowlist: EgressAllowlist, store: EgressSink, *, run_id: str | None = None,
                 shard_id: str | None = None, clock: Callable[[], str] = now):
        self.allowlist, self.store = allowlist, store
        self.run_id, self.shard_id, self._clock = run_id, shard_id, clock
        self.findings: list[Finding] = []
        self._raised: set[str] = set()

    @contextlib.contextmanager
    def context(self, **fields: Any) -> Iterator[None]:
        """Row fields for the requests made inside the block, in this thread or task (a context variable); an inner
        value wins over an outer one."""
        bad = set(fields) - set(ROW_FIELDS)
        if bad:
            raise ValueError(f"unknown egress row fields {sorted(bad)}")
        token = _CONTEXT.set({**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}})
        try:
            yield
        finally:
            _CONTEXT.reset(token)

    @contextlib.contextmanager
    def default_context(self, **fields: Any) -> Iterator[None]:
        """Like `context`, but an outer value wins: what a transport knows by default (its purpose, its model)."""
        outer = _CONTEXT.get()
        with self.context(**{k: v for k, v in fields.items() if k not in outer}):
            yield

    def _row(self, host: str, purpose: str, status: str, nbytes: int, ctx: Mapping[str, Any], **kw: Any) -> EgressRow:
        row = EgressRow(destination=normalize_host(host), purpose=purpose, status=status, bytes=max(0, int(nbytes)),
                        sent_at=self._clock(), run_id=self.run_id, shard_id=self.shard_id,
                        checkpoint_id=ctx.get("checkpoint_id"), artifact_sha256=ctx.get("artifact_sha256"),
                        redaction_version=ctx.get("redaction_version"), model_id=ctx.get("model_id"), **kw)
        self.store.log_egress(row)
        return row

    def _refuse(self, host: str, purpose: str, port: int, reason: str, nbytes: int, ctx: Mapping[str, Any]) -> None:
        self._row(host, purpose, "refused", nbytes, ctx, refusal_reason=reason)
        key = json.dumps([normalize_host(host), port, purpose])
        if key not in self._raised:
            self._raised.add(key)
            run = self.run_id or "run-local"
            f = Finding(finding_id="fnd-" + hashlib.sha256(f"{run}:egress_refused:{key}".encode()).hexdigest()[:16],
                        run_id=run, kind="harness_error", description=f"egress refused: {reason} ({purpose})",
                        first_seen_run_id=run, checkpoint_id=ctx.get("checkpoint_id"), severity="S3",
                        detail="egress_refused", evidence={"destination": normalize_host(host), "port": port,
                                                           "purpose": purpose})
            self.findings.append(f)
            if self.run_id is not None:
                self.store.write_finding(f)
        raise EgressRefused(normalize_host(host), purpose, reason)

    def request(self, host: str, send: Callable[[], T], *, purpose: str | None = None, port: int = 443,
                nbytes: int = 0, status_of: Callable[[T], int | None] = lambda r: None,
                request_id_of: Callable[[T], str | None] = lambda r: None, **fields: Any) -> T:
        """Check, send and log one outbound request (6.10). `nbytes` is the payload size; `fields` are row fields
        (`ROW_FIELDS`) over the context; `image=True` marks a payload that is a picture of the screen. Raises
        EgressRefused before `send` is called when the request is not permitted."""
        with self.context(**fields, **({"purpose": purpose} if purpose else {})):
            ctx = dict(_CONTEXT.get())
        p = ctx.get("purpose")
        if p not in EGRESS_PURPOSES:
            raise ValueError(f"an egress request names its purpose, one of {EGRESS_PURPOSES}; got {p!r}")
        reason = self.allowlist.refusal(host, p, port)
        if reason is None and ctx.get("image") and not ctx.get("redaction_version"):
            reason = "redaction version missing"                  # 8.9
        if reason is None and ctx.get("image") and p in TEXT_ONLY_PURPOSES:
            reason = f"images never go to the {p} host (6.10, 8.7)"
        if reason is not None:
            self._refuse(host, p, port, reason, nbytes, ctx)
        try:
            response = send()
        except Exception as e:
            self._row(host, p, "failed", nbytes, ctx, response_status=getattr(e, "status_code", None))
            raise
        self._row(host, p, "sent", nbytes, ctx, response_status=status_of(response),
                  request_id=request_id_of(response))
        return response


def refuse_all() -> EgressGate:
    """A gate over the empty allowlist with a `LogSink`: every request is refused before connecting (choice 6)."""
    return EgressGate(EgressAllowlist.empty(), LogSink())


# -- Nightly reconciliation (6.10) --------------------------------------------------------------------------------

@dataclass(frozen=True)
class FirewallEntry:
    """One rig firewall log line as the per-run collector hands it over (7.12): when, where to, permitted or
    blocked, and whether a harness process (not the app) opened it."""
    at: str
    host: str
    action: str
    harness: bool
    port: int = 443
    rig_id: str | None = None
    run_id: str | None = None
    process: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ("permitted", "blocked"):
            raise ValueError(f"firewall action {self.action!r}")


@dataclass(frozen=True)
class Reconciliation:
    """What the Nightly comparison found (6.10)."""
    unapproved_sent: tuple[EgressRow, ...] = ()
    unlogged: tuple[FirewallEntry, ...] = ()
    blocked: tuple[FirewallEntry, ...] = ()
    runs: tuple[str, ...] = ()
    findings: tuple[FindingSpec, ...] = ()

    @property
    def incident(self) -> str | None:
        return "S1" if self.unapproved_sent or self.unlogged else "S3" if self.blocked else None

    @property
    def verdict(self) -> RunVerdict | None:
        """The run outcome each run of `runs` is re-marked with, its report withheld."""
        return RunVerdict("aborted_harness", "egress_unlogged") if self.runs else None

    def control(self) -> ControlResult:
        """The Nightly control: S1 pages by the safety routing with the reports withheld; S3 is a harness_error."""
        s1 = self.incident == "S1"
        return ControlResult(
            "egress_reconciliation", self.incident is None, None, alert=alerts.safety_abort() if s1 else
            alerts.harness_error() if self.blocked else None, actions=("withhold_report",) if self.runs else (),
            findings=self.findings, incident=self.incident, evidence={"runs": list(self.runs)},
            detail=(f"{len(self.unapproved_sent)} sent to unapproved hosts, {len(self.unlogged)} unlogged "
                    f"connections, {len(self.blocked)} firewall blocks") if self.incident else None)


def _t(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def reconcile(rows: Sequence[EgressRow], allowlist: EgressAllowlist, firewall_log: Sequence[FirewallEntry], *,
              window_s: float = 60.0) -> Reconciliation:
    """Compare a day's egress rows with the allowlist and the rigs' firewall logs (6.10; choice 4)."""
    unapproved = tuple(r for r in rows if r.status == "sent" and not allowlist.allows_host(r.destination))
    logged = [r for r in rows if r.status in ("sent", "failed")]
    unlogged, blocked = [], []
    for e in firewall_log:
        if not e.harness:
            continue
        if e.action == "blocked":
            blocked.append(e)
            continue
        host, at = normalize_host(e.host), _t(e.at)
        if not any(normalize_host(r.destination) == host and abs((_t(r.sent_at) - at).total_seconds()) <= window_s
                   and (r.run_id is None or e.run_id is None or r.run_id == e.run_id) for r in logged):
            unlogged.append(e)
    findings = [FindingSpec("harness_error", f"egress sent to {r.destination}, a host outside the allowlist "
                                             f"({r.purpose})", severity="S1", detail="egress_unapproved_host")
                for r in unapproved]
    findings += [FindingSpec("harness_error", f"firewall-permitted connection to {e.host} from "
                             f"{e.process or 'a harness process'} with no egress row", severity="S1",
                             detail="egress_unlogged") for e in unlogged]
    findings += [FindingSpec("harness_error", f"firewall blocked {e.process or 'a harness process'} connecting to "
                             f"{e.host}", severity="S3", detail="firewall_block") for e in blocked]
    runs = sorted({r.run_id for r in unapproved if r.run_id} | {e.run_id for e in unlogged if e.run_id})
    return Reconciliation(unapproved, tuple(unlogged), tuple(blocked), tuple(runs), tuple(findings))


__all__ = ["AllowedHost", "AllowlistError", "EgressAllowlist", "EgressGate", "EgressRefused", "EgressSink",
           "FirewallEntry", "LogSink", "MODEL_PURPOSES", "Reconciliation", "allowlist_problems", "normalize_host",
           "reconcile", "refuse_all"]
