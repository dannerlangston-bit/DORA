"""What a finished simulated run hands the integrations: its alerts, Slack finding posts and Jira jobs (9.2, 9.9,
9.10, 1.5).

- `run_alerts(store, run_id, entries)`: the run's control-result routes (`orchestration.alert_entry`, one per shard
  and result) collapsed into one alert per run, rule and reason (`report.alerting.raise_run_alerts`), with the 9.10
  repeat escalation against the previous run of the same type (`escalate_repeats`, every gate, 11.6). Their entries are
  the run's record: `alerts.json` and `summary_json["alerts"]`.
- `after_run(store, cfg, run_id, ...)`: once the run row is final, every alert through the `Dispatcher` (pages, P1
  email, the on-call posts, released at 06:00 after hours), the tier-1 finding posts to the triage channel
  (`report.slack.plan_finding_posts`, the PR-run filter of 9.9 on PR runs), the SENT tasks of 9.9 row 4 (env_error
  rows by gate, harness_error rows by class, an aborted run by its reason, the unregistered_surface,
  dependency_map_gap, unknown_popup and missed_in_pr_run findings), the seen-again comment on the open ticket of each
  inherited confirm_bug, and the harness freeze of 9.10 (the second aborted_harness run in 7 days). Every job is an
  outbox row; its payload bytes are stored as the artifact its `payload_sha256` names, in the run's set. Returns the
  counts the Integrations row prints (9.2: "Jira {n} queued, Slack {n} queued").

Choices (reported):

1. The Dispatcher is built per run from `policy/reporting.yaml` (the four channels, 9.9; the configured names), the
   on-call roster (`ops/oncall.yaml`) and the operating day (`schedule/windows.yaml`); its dedup state lives for the
   run, so a later run's alert with the same key is sent again (the run id is in every key).
2. The integrations run after `end_run`, when the run's status is final; their payloads are not part of the 6.9
   teardown scan, which runs before (an alert or post carries no secret: its text comes from control-result details,
   finding descriptions and run ids).
3. SENT subjects: an env_error row's gate, a harness_error row's reason, the run's reason for an aborted run, the
   surface of an unregistered_surface finding's checkpoint, the description of a dependency_map_gap finding, the
   title an unknown_popup finding's evidence names (else its description), the checkpoint of a missed_in_pr_run
   finding. Synthetic rows and findings never open a task (9.9, 10.3).
4. Payloads are stored in the run's artifact set; a set security quarantined (a 6.9 leak) takes no more puts, so
   that run's payloads go to the `outbox/<run_id>` set beside it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..records import Finding
from ..report import jira, jira_sync, slack
from ..report.alerting import Alert, Dispatcher, RunRef, escalate_repeats, harness_freeze, raise_run_alerts
from ..store import ArtifactStore, Store
from ..taxonomy import checkpoint_parts
from .config import SentinelConfig

REPORT_BASE = "https://sentinel.internal"
ABORTS = ("aborted_safety", "aborted_budget", "aborted_harness")
FINDING_TRIGGERS = ("unregistered_surface", "dependency_map_gap", "unknown_popup", "missed_in_pr_run")


def raised(store: Store, run_id: str, entries: Iterable[Mapping[str, Any]]) -> tuple[Alert, ...]:
    """The run's alerts: one per run, rule and reason, escalated on a repeat (module docstring)."""
    run = RunRef.from_store(store, run_id, base_url=REPORT_BASE)
    return escalate_repeats(store, run, raise_run_alerts(run, entries))


def run_alerts(store: Store, run_id: str, entries: Iterable[Mapping[str, Any]]) -> list[dict]:
    """The `alerts.json` and summary `alerts` entries of the run (module docstring)."""
    return [a.entry() for a in raised(store, run_id, entries)]


def dispatcher(store: Store, cfg_repo: Any, cfg: SentinelConfig | None = None) -> Dispatcher:
    """The run's Dispatcher (choice 1): the configured channels, the on-call roster, the operating day."""
    rep = cfg_repo.one("reporting") if cfg_repo is not None else None
    doc = rep.data if rep is not None and isinstance(rep.data, dict) else None
    channels = slack.SlackChannels.from_reporting(doc) if doc else slack.SlackChannels()
    kw: dict[str, Any] = {"channels": channels}
    if cfg is not None:
        kw["oncall"] = cfg.oncall
        if cfg.windows is not None:
            kw["windows"] = cfg.windows
    return Dispatcher(store, **kw)


@dataclass
class IntegrationResult:
    """What `after_run` queued: the dispatched alerts, the Slack and Jira jobs, the harness freeze it set."""

    alerts: tuple[Any, ...] = ()
    slack_jobs: int = 0
    jira_jobs: int = 0
    jira_queued: int = 0
    freeze: str | None = None

    def integrations(self, base: Mapping[str, Any]) -> dict[str, Any]:
        """Run health's Integrations block (9.2) with the run's queued jobs."""
        out = dict(base)
        out["jira_queued"] = self.jira_queued
        out["slack"] = {**dict(base.get("slack") or {}), "queued": self.slack_jobs}
        return out


def _payload(arts: ArtifactStore | None, sha: str) -> bytes | None:
    """An outbox payload's bytes by its `payload_sha256` (tagged or bare), None when this run's set lacks it."""
    bare = sha.removeprefix("sha256:")
    return arts.get(bare) if arts is not None and arts.path_of(bare) else None


def _payload_set(arts: ArtifactStore, run_id: str) -> ArtifactStore:
    """Where a run's outbox payloads go (choice 4): its own set, or `outbox/<run_id>` beside it once security has
    quarantined the run's set (6.9: the hold is on the run's evidence; the page about the leak still goes out)."""
    if not arts.quarantined:                          # a property: the marker, or None
        return arts
    base = arts.root
    for _ in Path(arts.set_path).parts:
        base = base.parent
    return ArtifactStore.at(base, f"outbox/{run_id}", scanner=arts.scanner)


def _store_payloads(arts: ArtifactStore | None, run_id: str, queued: Iterable[Any]) -> int:
    n = 0
    for q in queued:
        if arts is not None:
            _payload_set(arts, run_id).put(q.payload, name=f"{run_id}/outbox/{q.job_id}.json", kind="request",
                                           media_type="application/json",
                                           expected_sha256=q.payload_sha256.removeprefix("sha256:"))
        n += 1
    return n


def sent_events(store: Store, run_id: str, status: str, reason: str | None,
                findings: Sequence[Finding]) -> list[jira.SentEvent]:
    """The 9.9 row 4 SENT events of a finished run (choice 3), one per trigger and subject."""
    events: dict[tuple[str, str], jira.SentEvent] = {}

    def add(trigger: str, subject: str | None, detail: str = "") -> None:
        if subject and " ".join(str(subject).split()):
            events.setdefault((trigger, str(subject)), jira.SentEvent(trigger, str(subject), run_id, detail))
    for r in store.query("SELECT outcome, gate, reason, checkpoint_id FROM outcomes WHERE run_id = ? AND synthetic = 0 "
                         "AND outcome IN ('env_error', 'harness_error') ORDER BY checkpoint_id", (run_id,)):
        if r["outcome"] == "env_error":
            add("env_error", r["gate"] or "gate", f"{r['checkpoint_id']}: {r['reason'] or 'gate'}")
        else:
            add("harness_error", r["reason"] or "harness_error", str(r["checkpoint_id"]))
    if status in ABORTS:
        add(status, reason or status, f"run {run_id} ended {status}")
    for f in findings:
        if f.kind not in FINDING_TRIGGERS:
            continue
        if f.kind == "unregistered_surface":
            subject = checkpoint_parts(f.checkpoint_id)["surface"] if f.checkpoint_id else f.description
        elif f.kind == "unknown_popup":
            subject = str(((f.evidence or {}).get("raw_values") or {}).get("title") or f.description)
        elif f.kind == "missed_in_pr_run":
            subject = f.checkpoint_id or f.description
        else:
            subject = f.description
        add(f.kind, subject, f.description)
    return list(events.values())


def after_run(store: Store, cfg_repo: Any, cfg: SentinelConfig | None, run_id: str, *, raw_alerts: Sequence[Mapping],
              arts: ArtifactStore | None, now: datetime, build_id: str | None = None,
              publish: bool = True) -> IntegrationResult:
    """Dispatch, post, file and freeze for one finished run (module docstring); `now` is the run's end. With
    `publish` False (a report withheld, 6.9) the alerts and SENT tasks still go out, the finding posts and seen-again
    comments do not. Calling it again for the run queues nothing twice (every queue dedups by payload)."""
    at = now.isoformat()
    run = store.query("SELECT run_type, outcome, abort_reason FROM runs WHERE run_id = ?", (run_id,))[0]
    alerts = raised(store, run_id, raw_alerts)
    d = dispatcher(store, cfg_repo, cfg)
    sent = [d.dispatch(a, now) for a in alerts]
    _store_payloads(arts, run_id, (q for x in sent for q in x.jobs))
    synthetic = {r[0] for r in store.query("SELECT finding_id FROM findings WHERE run_id = ? AND synthetic = 1",
                                           (run_id,))}
    findings = [f for f in store.findings(run_id) if f.finding_id not in synthetic]
    posts = [slack.FindingPost.from_finding(f, run_type=run["run_type"]) for f in findings if f.tier == 1 and publish]
    if run["run_type"] == "pr":
        posts = [p for p in posts if slack.should_post_pr(p)]
    messages = slack.plan_finding_posts(posts, history=slack.SlackHistory.from_store(
        store, now, lambda sha: _payload(arts, sha)), now=now,
        base_url=REPORT_BASE) if posts else []
    slack_jobs = _store_payloads(arts, run_id, slack.enqueue(store, messages, d.channels, now) if messages else ())
    jira_q: list[Any] = []
    for ev in sent_events(store, run_id, run["outcome"], run["abort_reason"], findings):
        q = jira_sync.sent_task(store, ev, report_base=REPORT_BASE, at=at)
        if q is not None:
            jira_q.append(q)
    for f in findings if publish else ():                 # 9.9: seen again on the inherited confirm_bug's ticket
        dec = store.get_decision(f.decision_ref) if f.decision_ref else None
        if dec is not None and (q := jira_sync.seen_again(store, dec, f, run_id=run_id, build_id=build_id,
                                                           report_base=REPORT_BASE, at=at)) is not None:
            jira_q.append(q)
    jira_jobs = _store_payloads(arts, run_id, jira_q)
    freeze = harness_freeze(store, now)                   # 9.10, 1.5: after end_run, so this run counts
    return IntegrationResult(tuple(sent), slack_jobs, jira_jobs, jira_sync.jira_queued(store, run_id), freeze)


def canary_bugs(store: Store, bugs: Sequence[Mapping], *, run_id: str, arts: ArtifactStore | None,
                at: str) -> int:
    """The Nightly's canary SENT Bugs (9.9 row 5) as outbox jobs, payloads stored; returns how many were queued."""
    return _store_payloads(arts, run_id, jira_sync.canary_bugs(store, bugs, run_id=run_id, report_base=REPORT_BASE,
                                                              at=at))
