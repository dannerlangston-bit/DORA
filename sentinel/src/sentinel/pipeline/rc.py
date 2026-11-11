"""The RC-only stages of an RC run (1.9, 11.2 RC row): lineage base, tier B migration, chaos, soak, Explorer.

An RC run is a merge-scope run plus five stages no other run type has. The census, flows and sweeps on rigs 1 to 4
run through the normal pipeline; this module decides the rest, over ports, and returns what the run writes:

| Stage | Where | Function | Decides |
|---|---|---|---|
| Lineage base | Central Store | `rc_lineage.resolve` | stored approved set or re-capture; `needs_baseline (reason: lineage_missing)` |
| Migration | rigs 1 to 4 | `run_migration` | every first-launch and conversion dialog of each tier B workspace saved under the previous release, loaded on the candidate, captured as a checkpoint |
| Chaos | rig 6, T+0:20 to 1:00 | `orchestrator.chaos.run_chaos` | the 7.11 scenarios and their assertions |
| Soak | rig 5, T+0:20 to 6:20 | `run_soak` | 24 cycles against the lineage soak and the `soak.yaml` budgets |
| Explorer | rig 6, after the last scripted shard, to T+7:50 | `run_explorer` | the session, its candidate flows and their single replay |

`run_rc(store, ctx, ports)` runs them in that order and returns an `RcResult`: the checkpoint outcomes this module
decides (with the metadata of the checkpoints it creates), the findings, the control results the Orchestrator
applies, the step events, the run-health lines, the `rc` block of the run summary (which the next RC run reads: the
lineage soak's log signatures and the Explorer's pause rule), the report sections of 9.4 and the run verdict a stage
forces (`aborted_budget (reason: app_restart_budget)` from the soak, a kill-switch trip). On the rigs the stages run
in parallel shards; each stage function is callable on its own for that.

Choices (reported):
1. Migration workspaces are the tier B entries whose `saved_under_build` is the previous release (the RC's released
   base build, as L-WSP-03 and the Migration flow class check). A migration dialog's checkpoint is
   `flow/<dialog surface>/flow.migration.<workspace id>/d<nn>`; a dialog the popup catalog and the registry do not
   match is captured under `MIGRATION_UNMATCHED` with an `unknown_popup` finding (7.8). Its base is the lineage's
   approved image in stored mode; otherwise the released build never shows the dialog, so it is `needs_baseline
   (reason: new_surface)` (present on the candidate, absent on the base, 3.11 input B). A workspace that does not
   load gives its census checkpoints `env_error (gate: workspace_loaded, reason: precondition:workspace_loaded)`.
2. Soak cycle checkpoints are tier 1 whatever their surface's tier (10.11: a breach is tier 1). The budgets are
   evaluated once the loop has stopped, over every sample, cycle by cycle. A freeze or crash consumes a relaunch
   through `watchdog.app_failure`; the fourth app failure ends the loop: the rest of the cycles are `not_run (reason:
   budget)` and the run `aborted_budget (reason: app_restart_budget)`. The client log is "not available" when no
   cycle could locate it; a new log signature's finding sits on the cycle where it first appeared.
3. The Explorer stage checks, in order: DR-07 Branch A (else "Explorer: not available under DR-07 B"), the
   denied-action test on this build (else the stage is `not_run (reason: quarantine)`, 6.8 Proof), the model endpoint
   (else `not_run (reason: model_unavailable)`), the pause rule, the session window, and a rig 6 left shaped by the
   chaos suite (the session does not start). A signature seen on an earlier night is replayed under its earlier
   finding id (the same candidate path) and updates last seen instead of opening a finding.
4. Explorer precision counts every candidate emitted, a `lint_failed` one included (it did not reproduce). A
   candidate that fails its lint is held with triage state `open` (nobody replayed it) and listed under "Explorer:
   not reproduced" with its lint errors.
5. The Explorer stage itself has no checkpoint: its `not_run (reason: quarantine | model_unavailable)` and a
   teardown's `env_error (gate: teardown, reason: precondition:explorer_ledger_clean)` are keyed
   `stage:explorer_stage` in `RcResult.outcomes` (the `explorer_stage` selector of `results.SubsetOutcome`), and
   `records` leaves them to the Orchestrator's `explorer_stage` sink.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .. import explorer_findings as ef
from .. import soak as sk
from ..agent import agent_enabled
from ..agent.session import ExplorerLoop, ExplorerRun
from ..agent.tools import TOOLSET_SCHEMA_VERSION
from ..explorer_steering import SteeringTarget, steering_version
from ..orchestrator import chaos as ch
from ..orchestrator.budgets import ShardBudgets
from ..orchestrator.explorer import (NOT_AVAILABLE_DR07B, ExplorerMetrics, SessionWindow, explorer_teardown,
                                     not_available_dr07b, not_started_model_unavailable, pause_line)
from ..orchestrator.ports import TrafficShaper
from ..orchestrator.results import ControlResult, FindingSpec, Outcome, RunVerdict, SubsetOutcome, worse
from ..orchestrator.rig import CrashSignal
from ..orchestrator.watchdog import app_failure
from ..rc_lineage import LINEAGE, LineageBase, LineageSoak, lineage_soak, resolve
from ..records import CheckpointOutcome, Finding
from ..scope import PlannedCheckpoint
from ..taxonomy import checkpoint_parts

MIGRATION_UNMATCHED = "dialog.migration-unmatched"
SOAK_FLOW = "soak"


# -- what the stages create --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CheckpointMeta:
    """The record fields of a checkpoint an RC stage creates (scope.py plans none of them except the soak's)."""

    workspace_id: str
    persona: str
    data_mode: str
    tier: int
    flow_id: str | None = None
    step_id: str | None = None
    flow_class: str | None = None


def _record(run_id: str, cid: str, o: Outcome, m: CheckpointMeta, *, capture: str | None = None,
            assertion_ids: Sequence[str] = ()) -> CheckpointOutcome:
    return CheckpointOutcome(run_id=run_id, checkpoint_id=cid, outcome=o.outcome, reason=o.reason, gate=o.gate,
                             detail=o.detail, workspace_id=m.workspace_id, persona=m.persona, data_mode=m.data_mode,
                             tier=m.tier, flow_id=m.flow_id, step_id=m.step_id, flow_class=m.flow_class,
                             assertion_ids=list(assertion_ids) or None, side="candidate",
                             artifacts={"candidate": capture} if capture else None)


# -- migration (2.3, 2.6.3) --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class MigrationDialog:
    """A first-launch or conversion dialog seen while the candidate loaded the workspace: its title, the registry
    surface the popup catalog matched (None when unmatched) and its redacted capture."""

    title: str
    surface_id: str | None
    capture_sha256: str


@dataclass(frozen=True)
class MigrationLoad:
    """What loading one tier B workspace on the candidate showed."""

    loaded: bool
    dialogs: tuple[MigrationDialog, ...] = ()
    detail: str | None = None


class MigrationPort(Protocol):
    """Loads a workspace saved under the previous release on the candidate and captures every dialog (2.6.3)."""

    def load_on_candidate(self, workspace_id: str) -> MigrationLoad:
        """Copy the workspace into usergui, load it, and capture each first-launch or conversion dialog."""


def migration_workspaces(workspaces: Iterable[Mapping[str, Any]], previous_release: str | None) -> list[Mapping]:
    """The tier B workspaces saved under the previous release (choice 1)."""
    return [w for w in workspaces if w.get("tier") == "B" and previous_release is not None
            and w.get("saved_under_build") == previous_release]


@dataclass(frozen=True)
class MigrationCheckpoint:
    """One captured migration dialog: its checkpoint, workspace, title, surface, capture and base."""

    checkpoint_id: str
    workspace_id: str
    title: str
    surface_id: str
    capture_sha256: str
    base_image: str | None


@dataclass
class MigrationResult:
    """The migration stage: the captured dialogs, the outcomes it decides, the findings and the report rows."""

    checkpoints: list[MigrationCheckpoint] = field(default_factory=list)
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    meta: dict[str, CheckpointMeta] = field(default_factory=dict)
    findings: list[tuple[str | None, FindingSpec]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)


def run_migration(port: MigrationPort, workspaces: Sequence[Mapping[str, Any]], base: LineageBase,
                  scope: Iterable[str]) -> MigrationResult:
    """Load each migration workspace on the candidate and capture its dialogs (2.3, 2.6.3; choice 1)."""
    out = MigrationResult()
    scoped = list(scope)
    for w in workspaces:
        wid, persona = str(w["id"]), str(w.get("persona", "active_trader"))
        load = port.load_on_candidate(wid)
        flow_id = f"flow.migration.{wid}"
        if not load.loaded:
            o = Outcome("env_error", "precondition:workspace_loaded", "workspace_loaded",
                        detail=load.detail or f"{wid} did not load on the candidate")
            hit = [c for c in scoped if c.startswith("census/") and checkpoint_parts(c)["qualifier"].split("/")[0] == wid]
            out.outcomes.update({c: o for c in hit})
            out.rows.append({"workspace": wid, "loaded": False, "dialogs": [], "detail": o.detail})
            continue
        dialogs = []
        for n, d in enumerate(load.dialogs, 1):
            surface = d.surface_id or MIGRATION_UNMATCHED
            cid = f"flow/{surface}/{flow_id}/d{n:02d}"
            if d.surface_id is None:
                out.findings.append((cid, FindingSpec("unknown_popup", f"unmatched dialog {d.title!r} while loading "
                                                      f"{wid} on the candidate (7.8)", tier=1)))
            image = base.base_image(cid) if base.mode == "stored" else None
            out.checkpoints.append(MigrationCheckpoint(cid, wid, d.title, surface, d.capture_sha256, image))
            out.meta[cid] = CheckpointMeta(wid, persona, "masked_live", 1, flow_id, f"d{n:02d}", "standard")
            if image is None:
                out.outcomes[cid] = Outcome("needs_baseline", "new_surface",
                                            detail=f"migration dialog {d.title!r}: absent on the released build")
            dialogs.append({"checkpoint_id": cid, "title": d.title, "surface_id": surface})
        out.rows.append({"workspace": wid, "loaded": True, "dialogs": dialogs, "detail": None})
    return out


# -- soak (10.11) ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SoakCycle:
    """What one 15-minute cycle recorded on rig 5: the cycle checkpoint's capture, the sampler readings and clock
    hashes since the soak start, an app failure the Watchdog saw (crash, OOM, process exit), the client log slice
    (None when the log could not be located) and the re-login checkpoints it needed."""

    capture_sha256: str | None
    samples: tuple[sk.Sample, ...] = ()
    clock: tuple[sk.ClockSample, ...] = ()
    crash: CrashSignal | None = None
    log_slice: str | None = None
    relogins: int = 0


class SoakPort(Protocol):
    """Rig 5's activity loop: one cycle at a time; None when the soak cannot go on (the run's budget or deadline)."""

    def run_cycle(self, cycle: int) -> SoakCycle | None:
        """Run the loop for one cycle and capture its checkpoint."""


@dataclass
class SoakResult:
    """The soak stage (10.11): per cycle outcome (None: the normal diff path decides) and base capture, findings,
    control results, the run verdict it forces, samples, the section and the summary block."""

    surface_id: str
    outcomes: dict[str, Outcome | None] = field(default_factory=dict)
    base_captures: dict[str, str | None] = field(default_factory=dict)
    captures: dict[str, str | None] = field(default_factory=dict)
    assertion_ids: dict[str, tuple[str, ...]] = field(default_factory=dict)
    findings: list[tuple[str | None, FindingSpec]] = field(default_factory=list)
    consequences: list[ControlResult] = field(default_factory=list)
    run: RunVerdict | None = None
    samples: list[sk.Sample] = field(default_factory=list)
    section: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    health: list[str] = field(default_factory=list)

    def sample_rows(self, start: datetime) -> list[dict[str, Any]]:
        """The 8.3 `soak_samples` rows (`store.write_soak_samples`), timestamped from the soak start."""
        return [{"sampled_at": (start + timedelta(seconds=s.t_s)).astimezone(timezone.utc).isoformat()
                 .replace("+00:00", "Z"), "pid": s.pid, "metric": s.metric, "value": s.value} for s in self.samples]


def run_soak(port: SoakPort, surface_id: str, lineage: LineageSoak, budgets: sk.SoakBudgets, *,
             shard_budgets: ShardBudgets | None = None, xmx_bytes: float | None = None,
             cycles: int = sk.CYCLES) -> SoakResult:
    """The 24-cycle soak loop (10.11, 1.9; choice 2)."""
    out = SoakResult(surface_id)
    sb = shard_budgets or ShardBudgets()
    clock: list[sk.ClockSample] = []
    freezes: list[float] = []
    relaunches: list[float] = []
    app_failed: dict[int, Outcome] = {}
    logs: list[tuple[int, str]] = []
    relogins = reached = 0
    for n in range(1, cycles + 1):
        cyc = port.run_cycle(n)
        if cyc is None:
            out.health.append(f"soak stopped after cycle {reached} of {cycles}")
            break
        reached = n
        cid = sk.cycle_checkpoint(surface_id, n)
        out.captures[cid] = cyc.capture_sha256
        out.samples.extend(cyc.samples)
        clock.extend(cyc.clock)
        relogins += cyc.relogins
        if cyc.log_slice is not None:
            logs.append((n, cyc.log_slice))
        failures = [(f.detected_s, sk.freeze_result(sb)) for f in sk.freeze_detector(cyc.clock)]
        freezes += [t for t, _ in failures]
        if cyc.crash is not None:
            failures.append((n * sk.CYCLE_S, app_failure(cyc.crash, sb, phase="running")))
        for t, r in failures:
            out.consequences.append(r)
            out.findings += [(cid, f) for f in r.findings]
            if r.checkpoint is not None:
                app_failed.setdefault(n, r.checkpoint)
            if r.has("relaunch"):
                relaunches.append(t)
            if r.run is not None:
                out.run = worse(out.run, r.run)
        if out.run is not None:
            out.health.append(f"soak: app failure {sb.app_relaunches + 1} at cycle {n}; the rest of the loop is "
                              "not_run (budget)")
            break
    verdicts = sk.evaluate(out.samples, budgets, cycles=reached, xmx_bytes=xmx_bytes, freezes=freezes,
                           relaunches=relaunches, clock=clock)
    for v in verdicts:
        cid = sk.cycle_checkpoint(surface_id, v.cycle)
        out.outcomes[cid] = sk.cycle_outcome(v, app_failed=app_failed.get(v.cycle),
                                             lineage_comparable=lineage.comparable)
        out.assertion_ids[cid] = v.assertion_ids
        out.base_captures[cid] = lineage.capture(v.cycle)
    out.outcomes.update(sk.unreached(surface_id, reached, cycles))
    log_text = "\n".join(text for _, text in logs) if logs else None
    signatures = sk.log_signatures(log_text, lineage.log_signatures)
    first_cycle: dict[str, int] = {}
    for n, text in logs:
        for sig, _line, _header in sk.stack_signatures(text):
            first_cycle.setdefault(sig, n)
    for spec, sig in zip(signatures.findings(), signatures.new):
        out.findings.append((sk.cycle_checkpoint(surface_id, first_cycle[sig]), spec))
    out.section = sk.soak_section(out.samples, budgets, out.outcomes, lineage=list(lineage.samples),
                                  lineage_comparable=lineage.comparable, logs=signatures,
                                  relaunches=len(relaunches), freezes=len(freezes), xmx_bytes=xmx_bytes)
    out.section["relogins"] = relogins
    out.summary = {"cycles_reached": reached, "relaunches": len(relaunches), "freezes": len(freezes),
                   "relogins": relogins, "lineage_run_id": lineage.run_id, "lineage_comparable": lineage.comparable,
                   "log_signatures": sorted(signatures.counts), "log_signatures_available": signatures.available,
                   "gate_line": out.section["gate_line"]}
    out.health.append(signatures.line)
    if not lineage.comparable:
        out.health.append("soak: the lineage soak is not comparable (fingerprint differs or none recorded); candidate "
                          "curves shown alone")
    return out


# -- the Explorer stage (2.9, 6.8) -------------------------------------------------------------------------------
@dataclass
class ExplorerStage:
    """What the Explorer stage reads (choice 3). `loop` builds the session's tool loop for the window and the target
    list; `teardown` replays the mutation ledger and returns (items, verified deleted)."""

    branches: Mapping[str, Any]
    agent_available: bool
    deny_test_passed: bool
    history: Sequence[Mapping[str, Any]]
    steering: Sequence[SteeringTarget]
    window: SessionWindow
    loop: Callable[[SessionWindow, Sequence[SteeringTarget]], ExplorerLoop]
    runner: ef.ReplayRunner
    lint: Callable[[ef.CandidateFlow], list[str]]
    flow_ctx: ef.FlowContext
    controls: Iterable[str]
    build_id: str
    teardown: Callable[[], tuple[Sequence[str], Mapping[str, bool]]]
    prior: Mapping[str, ef.PriorFinding] = field(default_factory=dict)
    rig_clean: bool = True


@dataclass
class ExplorerResult:
    """The Explorer stage: whether a session ran, its replayed findings, the outcomes of the replayed checkpoints,
    the metrics, the control results, the step events, the health lines and the report section."""

    ran: bool = False
    line: str | None = None
    session: ExplorerRun | None = None
    replayed: list[ef.ReplayedFinding] = field(default_factory=list)
    outcomes: dict[str, Outcome] = field(default_factory=dict)
    meta: dict[str, CheckpointMeta] = field(default_factory=dict)
    captures: dict[str, str | None] = field(default_factory=dict)
    metrics: ExplorerMetrics | None = None
    consequences: list[ControlResult] = field(default_factory=list)
    health: list[str] = field(default_factory=list)
    section: dict[str, Any] = field(default_factory=dict)

    def findings(self, run_id: str, finding_id: Callable[[ef.ReplayedFinding], str]) -> list[Finding]:
        """The `explorer_candidate` findings to write: the new signatures only (a signature seen before updates its
        earlier finding's last seen through `explorer_findings.mark_seen_again`)."""
        trace = self.session.trace if self.session else []
        out = []
        for r in self.replayed:
            if r.seen_before is not None:
                continue
            out.append(ef.to_finding(r.candidate, finding_id=finding_id(r), run_id=run_id, replayed=r.replayed,
                                     candidate_flow=r.flow.path, held=r.outcome is None,
                                     triage_state=r.hold or "open", trace=trace))
        return out


def _not_started(result: ControlResult | None, line: str) -> ExplorerResult:
    r = ExplorerResult(line=line, health=[line], section={"line": line})
    if result is not None:
        r.consequences.append(result)
        r.outcomes.update({f"stage:{s.selector}": s.outcome for s in result.outcomes})
    return r


def run_explorer(stage: ExplorerStage, run_id: str) -> ExplorerResult:
    """The Explorer stage (2.9, 6.8; choice 3)."""
    if not agent_enabled(stage.branches):
        return _not_started(not_available_dr07b(), NOT_AVAILABLE_DR07B)
    if not stage.deny_test_passed:
        line = "Explorer: not run, the denied-action test has not passed on this build (6.8)"
        return _not_started(ControlResult("explorer", False, "running", detail=line, outcomes=(
            SubsetOutcome("explorer_stage", Outcome("not_run", "quarantine")),)), line)
    if not stage.agent_available:
        r = not_started_model_unavailable()
        return _not_started(r, r.detail or "Explorer: not run, model endpoint unavailable")
    sv = steering_version(stage.steering)
    paused = pause_line(stage.history, steering_version=sv, toolset_schema_version=TOOLSET_SCHEMA_VERSION)
    if paused:
        return _not_started(None, paused)
    if stage.window.start is None:
        return _not_started(None, stage.window.health)
    if not stage.rig_clean:
        return _not_started(None, "Explorer: not run, a chaos rule is still in force on rig 6")
    session = stage.loop(stage.window, stage.steering).run()
    out = ExplorerResult(ran=True, session=session, consequences=list(session.consequences))
    found = ef.candidates(session.findings, session_id=f"exp-{run_id}",
                          trace_ref=f"step_events:{run_id}", build_id=stage.build_id)
    refs = {c.key: (stage.prior[c.key].finding_id if c.key in stage.prior else f"exp-{run_id}-{c.seq:04d}")
            for c in found}
    out.replayed = ef.process(found, session.trace, run_id=run_id, ctx=stage.flow_ctx, controls=stage.controls,
                              runner=stage.runner, lint=stage.lint, prior=stage.prior, ref=lambda c: refs[c.key])
    for r in out.replayed:
        if r.outcome is not None:
            out.outcomes[r.flow.checkpoint_id] = r.outcome
            out.meta[r.flow.checkpoint_id] = CheckpointMeta(stage.flow_ctx.workspace, stage.flow_ctx.persona,
                                                            stage.flow_ctx.data_mode, stage.flow_ctx.tier,
                                                            r.flow.flow_id, ef.CHECKPOINT_STEP, "explorer_candidate")
            out.captures[r.flow.checkpoint_id] = r.result.capture_sha256 if r.result else None
    out.metrics = ExplorerMetrics(len(out.replayed), sum(r.replayed == "reproduced" for r in out.replayed),
                                  session.actions, session.agent_errors, session.deny_hits, session.budget_hit,
                                  session.usd, sv, TOOLSET_SCHEMA_VERSION)
    items, verified = stage.teardown()
    td = explorer_teardown(items, verified)
    if not td.passed:
        out.consequences.append(td)
        out.outcomes.update({f"stage:{s.selector}": s.outcome for s in td.outcomes})
    out.health = [stage.window.health, *out.metrics.health_lines()]
    if session.ended == "order_stage":
        out.health.append("Explorer: explorer_order_stage, the session ended (S1); the run continues")
    out.section = {
        "line": None,
        "reproduced": [{"checkpoint_id": r.flow.checkpoint_id, "outcome": r.outcome.outcome, "reason": r.outcome.reason,
                        "candidate_flow": r.flow.path, "surface_id": r.candidate.surface_id,
                        "observation": r.candidate.observation} for r in out.replayed if r.outcome is not None],
        ef.NOT_REPRODUCED: [{"candidate_flow": r.flow.path, "observation": r.candidate.observation,
                             "surface_id": r.candidate.surface_id, "held_as": r.hold or "open",
                             "replayed": r.replayed, "lint_errors": list(r.lint_errors)}
                            for r in out.replayed if r.outcome is None],
        "metrics": out.metrics.to_summary(), "ended": session.ended}
    return out


def explorer_history(store: Any, *, before: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """The earlier finished RC runs' `rc.explorer` summaries, newest first (the pause rule's input)."""
    sql = ("SELECT run_id, started_at, summary_json FROM runs WHERE run_type = 'rc' AND lifecycle = 'finished'"
           + (" AND started_at < ?" if before else "") + " ORDER BY started_at DESC, run_id DESC LIMIT ?")
    rows = store.query(sql, ((before,) if before else ()) + (limit,))
    out = []
    for r in rows:
        block = (store.run_summary(r["run_id"]).get("rc") or {}).get("explorer")
        if isinstance(block, Mapping):
            out.append(dict(block))
    return out


# -- the whole RC stage set ----------------------------------------------------------------------------------------
@dataclass
class RcContext:
    """What the RC stages read: the run, the live fingerprint, the scope, the corpus workspaces, the soak surface
    (scope.py's `SoakPlan.surface_id`), the budgets, the chaos plan and the Explorer stage inputs (None only when
    the Explorer is not built, DR-07 Branch B: the report then prints the "not available" line)."""

    run_id: str
    fingerprint_id: str
    scope: Sequence[str]
    workspaces: Sequence[Mapping[str, Any]]
    soak_surface_id: str
    soak_budgets: sk.SoakBudgets = field(default_factory=sk.SoakBudgets)
    released_build_id: str | None = None
    lineage: str = LINEAGE
    xmx_bytes: float | None = None
    chaos_plan: ch.ChaosPlan | None = None
    chaos_skip: Mapping[str, Outcome] = field(default_factory=dict)
    explorer: ExplorerStage | None = None


@dataclass
class RcPorts:
    """The rig sides of the stages: rigs 1 to 4 for migration, rig 5 for the soak, rig 6 for chaos."""

    migration: MigrationPort
    soak: SoakPort
    shaper: TrafficShaper
    chaos: ch.ChaosDriver


@dataclass
class RcResult:
    """Everything the RC stages decided (module docstring)."""

    base: LineageBase
    migration: MigrationResult
    chaos: ch.ChaosSuiteResult | None
    soak: SoakResult
    explorer: ExplorerResult | None
    run: RunVerdict | None = None
    health: list[str] = field(default_factory=list)
    chaos_plan: ch.ChaosPlan | None = None

    @property
    def outcomes(self) -> dict[str, Outcome]:
        """Checkpoint id -> the outcome an RC stage decided (`stage:explorer_stage` for the Explorer stage itself)."""
        out: dict[str, Outcome] = dict(self.base.outcomes())
        out.update(self.migration.outcomes)
        if self.chaos is not None:
            out.update({r.checkpoint_id: r.outcome for r in self.chaos.results if r.outcome is not None})
        out.update({c: o for c, o in self.soak.outcomes.items() if o is not None})
        if self.explorer is not None:
            out.update(self.explorer.outcomes)
        return out

    def meta(self, soak_meta: CheckpointMeta) -> dict[str, CheckpointMeta]:
        """The record metadata of every checkpoint the stages created; `soak_meta` is the soak workspace's."""
        out = dict(self.migration.meta)
        if self.chaos is not None:
            p = self.chaos_plan
            out.update({r.checkpoint_id: CheckpointMeta(p.workspace_id, p.persona, p.data_mode, ch.CHAOS_TIER,
                                                        r.scenario.flow_id, ch.CHECKPOINT_STEP, "standard")
                        for r in self.chaos.results})
        for cid in self.soak.outcomes:
            out[cid] = CheckpointMeta(soak_meta.workspace_id, soak_meta.persona, "masked_live", sk.SOAK_TIER,
                                      SOAK_FLOW, cid.rsplit("/", 1)[1], "standard")
        if self.explorer is not None:
            out.update(self.explorer.meta)
        return out

    def records(self, run_id: str, soak_meta: CheckpointMeta,
                planned: Mapping[str, PlannedCheckpoint] | None = None) -> tuple[list[CheckpointOutcome], list[str]]:
        """The outcome records of every decided checkpoint, and the ids neither the stages nor `planned` describe.
        Chaos records keep their failed assertions and lower results; soak records their breached assertions."""
        meta = self.meta(soak_meta)
        chaos_by_id = {r.checkpoint_id: r for r in self.chaos.results} if self.chaos is not None else {}
        out, unplaced = [], []
        for cid, o in self.outcomes.items():
            if cid.startswith("stage:"):
                continue
            if cid in chaos_by_id and self.chaos_plan is not None:
                rec = chaos_by_id[cid].record(run_id, self.chaos_plan)
                if rec is not None:
                    out.append(rec)
                continue
            if cid in meta:
                capture = self.soak.captures.get(cid) or (self.explorer.captures.get(cid) if self.explorer else None) \
                    or next((m.capture_sha256 for m in self.migration.checkpoints if m.checkpoint_id == cid), None)
                out.append(_record(run_id, cid, o, meta[cid], capture=capture,
                                   assertion_ids=self.soak.assertion_ids.get(cid, ())))
            elif planned and cid in planned:
                rec = planned[cid].outcome(run_id, o.outcome, o.reason, o.gate)
                rec.detail = o.detail
                out.append(rec)
            else:
                unplaced.append(cid)
        return out, unplaced

    def findings(self) -> list[tuple[str | None, FindingSpec]]:
        """Every finding spec with the checkpoint it sits on, the findings of `consequences()` included (an app
        failure's `app_crash` on its soak cycle, `explorer_order_stage` on none). The `explorer_candidate` findings are
        `ExplorerResult.findings`."""
        out = list(self.migration.findings) + list(self.soak.findings)
        if self.chaos is not None:
            out += [(r.checkpoint_id, f) for r in self.chaos.results for f in r.findings]
        if self.explorer is not None:
            out += [(None, f) for r in self.explorer.consequences for f in r.findings]
        return out

    def consequences(self) -> list[ControlResult]:
        """The control results the Orchestrator applies for their actions, alerts, incidents and run verdicts (a
        relaunch, `aborted_budget`, the kill-switch trip, a blocked Explorer login); their findings are already in
        `findings()`."""
        out = list(self.soak.consequences)
        if self.explorer is not None:
            out += self.explorer.consequences
        return out

    def summary(self) -> dict[str, Any]:
        """The `rc` block of `runs.summary_json` (`store.update_summary(run_id, result.summary())`)."""
        block: dict[str, Any] = {"lineage": self.base.to_summary(), "soak": self.soak.summary,
                                 "migration": self.migration.rows}
        if self.chaos is not None:
            block["chaos"] = [r.report_row() for r in self.chaos.results]
        if self.explorer is not None:
            block["explorer"] = self.explorer.metrics.to_summary() if self.explorer.metrics else \
                {"ran": False, "line": self.explorer.line}
        return {"rc": block}

    def sections(self) -> dict[str, Any]:
        """The RC report's sections this module feeds (9.4): lineage comparison, tier B migration results, the
        connectivity (chaos) section, the soak section and the Explorer section."""
        return {"lineage": self.base.to_summary(), "migration": self.migration.rows,
                "connectivity": [r.report_row() for r in self.chaos.results] if self.chaos else [],
                "soak": self.soak.section,
                "explorer": self.explorer.section if self.explorer else {"line": NOT_AVAILABLE_DR07B}}


def run_rc(store: Any, ctx: RcContext, ports: RcPorts) -> RcResult:
    """Every RC-only stage, in schedule order (module docstring)."""
    base = resolve(store, ctx.lineage, fingerprint_id=ctx.fingerprint_id, scope=ctx.scope,
                   released_build_id=ctx.released_build_id)
    released = base.released_build_id
    migration = run_migration(ports.migration, migration_workspaces(ctx.workspaces, released), base, ctx.scope)
    chaos_res = None
    if ctx.chaos_plan is not None:
        chaos_res = ch.run_chaos(ports.shaper, ports.chaos, ctx.chaos_plan, skip=ctx.chaos_skip)
    soak_res = run_soak(ports.soak, ctx.soak_surface_id, lineage_soak(store, released,
                                                                      fingerprint_id=ctx.fingerprint_id),
                        ctx.soak_budgets, xmx_bytes=ctx.xmx_bytes)
    explorer_res = None
    if ctx.explorer is not None:
        clean = ctx.explorer.rig_clean and (chaos_res is None or chaos_res.rig_clean)
        explorer_res = run_explorer(replace(ctx.explorer, rig_clean=clean), ctx.run_id)
    run = soak_res.run
    for r in (explorer_res.consequences if explorer_res else []):
        run = worse(run, r.run)
    health = [base.health_line()] + (list(chaos_res.health) if chaos_res else []) + soak_res.health \
        + (explorer_res.health if explorer_res else [])
    return RcResult(base, migration, chaos_res, soak_res, explorer_res, run, health, ctx.chaos_plan)


def soak_meta(workspaces: Iterable[Mapping[str, Any]], workspace_id: str) -> CheckpointMeta:
    """The soak cycles' record metadata from the soak workspace's corpus entry (`soak.soak_workspace`)."""
    w = next((x for x in workspaces if x.get("id") == workspace_id), {})
    return CheckpointMeta(workspace_id, str(w.get("persona", "active_trader")), "masked_live", sk.SOAK_TIER,
                          SOAK_FLOW, None, "standard")
