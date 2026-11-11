"""The Calibration run (1.6 Calibration row, 3.1, 3.9 steps 1 and 2, 5.15, 7.13, 11.2): `run_calibration`, and
`sentinel calibrate` (`main`).

A Calibration run captures one build: per checkpoint five captures in one session on one rig (r1 to r5) and a sixth
the following night in a fresh session on the same rig (r6), through the same settle, redaction and landmark steps
as a product run; every checkpoint keeps to one rig while the library is sharded across the pool (rigs 1 to 4) in
one weekend `all_quiet` window. It produces statistics, no classification:

1. The run row (8.3 `runs`, run_type `calibration`) with one shard per rig and night and every checkpoint registered
   (8.3 `checkpoints`), but no scope rows: a Calibration run writes no outcome rows (3.1).
2. Each session passes the Orchestrator's gates through `fixture_verified` and `canary_flow_passed` (5.2) and records
   its fingerprint (3.9 step 1, 8.3 env_fingerprints), then captures its shard: night 1 five passes over the shard's
   checkpoints, night 2 one. A session that stops at a gate, or before a capture, gives that capture the session's
   outcome (`env_error (gate)` when it is not one of the four outcomes of 3.9 step 2); a night-2 session whose
   rendering key differs from night 1's gives r6 `env_error (rig_spec_mismatch)`, because the six captures of a
   checkpoint share one calibration key (7.13). Each such capture is a fix-list item (3.9 step 2), never dropped.
3. `diff.calibration.calibrate_checkpoint` per checkpoint (3.9 steps 3 to 5) with the checkpoint's parking
   rectangle, redaction boxes, tier, owner and canary mutant crops; `diff.calibration.write_calibration` writes
   `calibration_floors` and `calibration_fixes` keyed by the rendering key with what each floor was measured under
   (SD-19, SD-20, SD-69, 7.13), and `diff.calibration.export_file` writes `calibration/<hex>.yaml` (4.16, SD-36).
4. Run health gets the fix list by owner (1.6: "fix list of non-zero frozen floors to flow owners") with each item's
   cause, standard fix and age (3.9 step 6), and the items a frozen checkpoint has carried for more than 5 business
   days (10.5); the run ends with the worst shard's outcome (8.2).

The rig is behind two small interfaces built per shard and night by a `RigFactory`: `CalibrationSession.run(work)`
mirrors the Orchestrator's `run(work)`, and `SessionContext` gives `capture` and `registry` while the session is in
`running`. `SimRigs` is the factory over the simulated rig (`pipeline.orchestration.shard_setup`, the
Orchestrator's gates, `pipeline.sim.SimApp` screens, `diff.settle`, `capture.redact_pair`); `main` uses it, since
the simulated rig is the only one without Windows.

Choices where the spec is ambiguous (reported):

1. "Five times back to back in one session" (3.9) and "five passes" (11.2): r1 to r5 are five passes over the shard's
   checkpoints in plan order inside one session, so every capture reaches its checkpoint afresh.
2. A night-2 fingerprint with another rendering key than night 1's (an overnight Windows update) gives r6
   `env_error (reason: rig_spec_mismatch)` on every checkpoint of the shard.
3. A shard with no recorded fingerprint (its sessions stopped at a gate) has no rendering key: its fix items are
   written without floors and belong to no key (`diff.calibration` choice 8).
4. Fix items are opened at `now`, by default the start of the second night (the sixth capture completes the set);
   the floor exceptions in force are read from the config's regions files at `now`.
5. The plan of `sentinel calibrate` is a JSON file (`CalibrationPlan.to_dict`): run id, build, window, second night
   and per rig its checkpoints with their owners; `scope.plan_calibration` is what produces one. The simulated
   rig's fingerprint uses the PR run's harness fields (`pipeline.run`), so a PR run on the same simulated
   rig finds these floors under its calibration key.
6. A checkpoint with a capture that ended `env_error` carries that outcome (3.9 step 2), so 1.10's
   `complete_with_env_errors` applies: a shard with such a capture, its r6 replaced on a changed key (choice 2)
   included, ends at least `complete_with_env_errors` with no run reason (the `env_error` checkpoint is the cause),
   and so does the run (8.2, worst of the shards); `sentinel calibrate` then exits 1. Other failed captures
   (`not_settled`, `landmark_missing`, `harness_error`) are fix items only and leave the run `complete`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from .canonical import calibration_key
from .diff import load_params
from .diff.calibration import (
    Capture, CheckpointCalibration, FixItem, LiveVersions, StoreCalibration, calibrate_checkpoint,
    checkpoint_versions, epoch_definition_sha256, export_file, fix_list_lines, fix_rows, live_exceptions,
    mask_version, overdue, write_calibration,
)
from .diff.regions import RedactionBox, RegionsFile, park_rect
from .records import Fingerprint, RunOutcome
from .scope import PlannedCheckpoint
from .store.store import RUN_SEVERITY, RunPlan, ScopeEntry, ShardPlan, Store

NY = ZoneInfo("America/New_York")
FAILED = ("not_settled", "landmark_missing", "env_error", "harness_error")      # 3.9 step 2
GATE_FAILURE = ("env_error", "gate")
KEY_CHANGED = ("env_error", "rig_spec_mismatch")                                # choice 2
ENV_ERRORS = ("complete_with_env_errors", "env_error")                          # 1.10, choice 6


# ------------------------------------------------------------------------------------------------
# The plan

@dataclass(frozen=True)
class CalibrationItem:
    """One checkpoint of the plan and the owner its fix items go to (the flow owner, else the surface owner)."""

    checkpoint: PlannedCheckpoint
    owner: str


@dataclass(frozen=True)
class CalibrationShard:
    """One rig and its checkpoints, in capture order: each checkpoint's six captures stay on this rig (3.9 step 2)."""

    rig_id: str
    items: tuple[CalibrationItem, ...]


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CalibrationPlan:
    """A Calibration run's request (1.6, 3.9, 11.2): the build, the shards, the weekend `all_quiet` window (r1 to r5
    start at `window_start`, the run's wall budget ends at `window_end`) and the second night's session start (r6)."""

    run_id: str
    build_id: str
    shards: tuple[CalibrationShard, ...]
    window_start: datetime
    window_end: datetime
    second_night: datetime
    config_commit: str = "unknown"
    config_tarball_sha256: str = "0" * 64
    snapshot_set_sha256: str = "0" * 64
    harness_sha: str = "unknown"
    authorization_ref: str = "unknown"

    def __post_init__(self) -> None:
        rigs = [s.rig_id for s in self.shards]
        if not rigs or len(rigs) != len(set(rigs)):
            raise ValueError("a Calibration plan has one shard per rig, at least one (3.9 step 2)")
        cids = [i.checkpoint.checkpoint_id for s in self.shards for i in s.items]
        twice = sorted({c for c in cids if cids.count(c) > 1})
        if twice:
            raise ValueError(f"checkpoints on more than one rig, or twice on one: {twice} (3.9: five captures on one "
                             "rig per checkpoint)")
        start, night, end = (_aware(t) for t in (self.window_start, self.second_night, self.window_end))
        if not start < night < end:
            raise ValueError("the second night falls inside the window, after its start (11.2)")
        if night.astimezone(NY).date() <= start.astimezone(NY).date():
            raise ValueError("r6 is captured the following night, in a fresh session (3.9 step 2)")

    @property
    def items(self) -> list[CalibrationItem]:
        return [i for s in self.shards for i in s.items]

    def to_dict(self) -> dict:
        """The plan file of `sentinel calibrate` (choice 5)."""
        def cp(i: CalibrationItem) -> dict:
            c = i.checkpoint
            return {"checkpoint_id": c.checkpoint_id, "surface_id": c.surface_id, "test_kind": c.test_kind,
                    "tier": c.tier, "workspace_id": c.workspace_id, "persona": c.persona, "data_mode": c.data_mode,
                    "flow_id": c.flow_id, "step_id": c.step_id,
                    "size_step": dict(c.size_step) if c.size_step else None, "owner": i.owner}
        return {"run_id": self.run_id, "build_id": self.build_id, "window_start": self.window_start.isoformat(),
                "window_end": self.window_end.isoformat(), "second_night": self.second_night.isoformat(),
                "config_commit": self.config_commit, "config_tarball_sha256": self.config_tarball_sha256,
                "snapshot_set_sha256": self.snapshot_set_sha256, "harness_sha": self.harness_sha,
                "authorization_ref": self.authorization_ref,
                "shards": [{"rig_id": s.rig_id, "checkpoints": [cp(i) for i in s.items]} for s in self.shards]}

    @staticmethod
    def from_dict(d: Mapping[str, Any]) -> "CalibrationPlan":
        def item(c: Mapping[str, Any]) -> CalibrationItem:
            step = c.get("size_step")
            return CalibrationItem(PlannedCheckpoint(
                str(c["checkpoint_id"]), str(c["surface_id"]), str(c["test_kind"]), int(c["tier"]),
                str(c["workspace_id"]), str(c["persona"]), str(c["data_mode"]), c.get("flow_id"), c.get("step_id"),
                tuple(sorted(step.items())) if step else None), str(c["owner"]))
        extra = {k: str(d[k]) for k in ("config_commit", "config_tarball_sha256", "snapshot_set_sha256", "harness_sha",
                                        "authorization_ref") if d.get(k)}
        return CalibrationPlan(
            str(d["run_id"]), str(d["build_id"]),
            tuple(CalibrationShard(str(s["rig_id"]), tuple(item(c) for c in s.get("checkpoints") or ()))
                  for s in d.get("shards") or ()),
            datetime.fromisoformat(str(d["window_start"])), datetime.fromisoformat(str(d["window_end"])),
            datetime.fromisoformat(str(d["second_night"])), **extra)


def owner_of(cfg: Any, checkpoint: PlannedCheckpoint) -> str:
    """The owner a checkpoint's fix items go to (1.6): its flow's owner, else its surface's, else `unassigned`."""
    if checkpoint.flow_id:
        owner = (cfg.flow_docs.get(checkpoint.flow_id) or {}).get("owner")
        if owner:
            return str(owner)
    return str((cfg.surfaces.get(checkpoint.surface_id) or {}).get("owner") or "unassigned")


def items_for(cfg: Any, planned: Iterable[PlannedCheckpoint]) -> tuple[CalibrationItem, ...]:
    """Calibration items for planned checkpoints, each with its owner (`owner_of`)."""
    return tuple(CalibrationItem(cp, owner_of(cfg, cp)) for cp in planned)


# ------------------------------------------------------------------------------------------------
# The rig interfaces

@dataclass(frozen=True)
class Shot:
    """One calibration capture as the rig returns it: the redacted frame and its time to settle, or a failure with
    one of the four outcomes of 3.9 step 2; plus the redaction boxes and the parking coordinate it was taken with."""

    image: np.ndarray | None
    outcome: str | None = None
    reason: str | None = None
    time_to_settle_ms: int | None = None
    redactions: tuple[RedactionBox, ...] = ()
    park: tuple[int, int] | None = None


class SessionContext(Protocol):
    """What a session in `running` gives the run: a capture of one checkpoint (`index` 0 to 5 is r1 to r6) and the
    checkpoint's effective Region Registry entry (4.8, after `effective_registry`)."""

    def capture(self, item: CalibrationItem, index: int) -> Shot: ...

    def registry(self, item: CalibrationItem) -> RegionsFile: ...


@dataclass(frozen=True)
class SessionReport:
    """How one session ended (5.2): its verdict and last gate, the fingerprint it recorded after attestation (3.9 step
    1), the outcome its unreached captures take when it stopped, its redaction version and when it started."""

    status: str
    last_gate: str
    reason: str | None = None
    fingerprint: Fingerprint | None = None
    stopped: tuple[str, str | None] | None = None
    redaction_version: str | None = None
    started_at: datetime | None = None


class CalibrationSession(Protocol):
    """One session on one rig: the gates through `canary_flow_passed`, then `work` in `running`, then teardown."""

    def run(self, work: Callable[[SessionContext], None]) -> SessionReport: ...


RigFactory = Callable[[CalibrationShard, int, str], CalibrationSession]   # (shard, night 1 or 2, shard id)


# ------------------------------------------------------------------------------------------------
# The run

@dataclass
class CalibrationResult:
    """What a Calibration run produced: its outcome, the calibration per checkpoint, the rendering key of each
    checkpoint's rig, the open fix items, the exported files and one entry per session."""

    outcome: RunOutcome
    calibrations: dict[str, CheckpointCalibration]
    keys: dict[str, str | None]
    fix_list: list[FixItem]
    files: list[Path] = field(default_factory=list)
    sessions: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)          # run health's `calibration` block (8.3 runs.summary_json)


def versions_for(cfg: Any, checkpoint: PlannedCheckpoint, registry: RegionsFile,
                 redaction_version: str | None) -> LiveVersions:
    """The 7.13 versions of a checkpoint from the config (`diff.calibration.checkpoint_versions`): its masks'
    record versions, its workspace's corpus hash and its epoch definition."""
    masks = {m.mask_id: mask_version(m) for m in cfg.masks.values() if m.checkpoint_id == checkpoint.checkpoint_id}
    ws = next((str(d.data.get("file_sha256")) for d in cfg.repo.of("workspace")
               if isinstance(d.data, dict) and d.data.get("id") == checkpoint.workspace_id
               and d.data.get("file_sha256")), None)
    return checkpoint_versions(registry, mask_versions=masks, redaction_version=redaction_version,
                               workspace_sha256=ws,
                               epoch_definition_sha256=epoch_definition_sha256(checkpoint.data_mode,
                                                                               cfg.fixture.get("epoch")))


def _flow_class(cfg: Any, cp: PlannedCheckpoint) -> str | None:
    if cp.test_kind != "flow":
        return None
    return str((cfg.flow_docs.get(cp.flow_id or "") or {}).get("class", "standard"))


def _capture(shot: Shot) -> Capture:
    if shot.image is None:
        outcome = shot.outcome if shot.outcome in FAILED else GATE_FAILURE[0]
        return Capture(None, outcome=outcome, reason=shot.reason if shot.outcome in FAILED else GATE_FAILURE[1],
                       time_to_settle_ms=shot.time_to_settle_ms)
    return Capture(shot.image, time_to_settle_ms=shot.time_to_settle_ms)


def _failed(stopped: tuple[str, str | None] | None) -> Shot:
    outcome, reason = stopped if stopped is not None and stopped[0] in FAILED else GATE_FAILURE
    return Shot(None, outcome, reason)


def _worst(reports: Sequence[SessionReport]) -> SessionReport:
    return max(reversed(reports), key=lambda r: RUN_SEVERITY.index(r.status))


def _key(report: SessionReport) -> str | None:
    """The rendering key of a session's recorded fingerprint, None when it recorded none (choice 3)."""
    return calibration_key(report.fingerprint.match) if report.fingerprint is not None else None


def _key_changed(night1: SessionReport, night2: SessionReport) -> bool:
    """Choice 2: the night-2 session recorded another rendering key than night 1's."""
    k1, k2 = _key(night1), _key(night2)
    return k1 is not None and k2 is not None and k1 != k2


def _raised(status: str, reason: str | None, env_error: bool) -> tuple[str, str | None]:
    """Choice 6: an outcome less severe than `complete_with_env_errors` becomes it (no run reason: an `env_error`
    checkpoint is its own cause, 1.10) when a capture ended `env_error`."""
    if env_error and RUN_SEVERITY.index(status) < RUN_SEVERITY.index(ENV_ERRORS[0]):
        return ENV_ERRORS[0], None
    return status, reason


def run_calibration(cfg: Any, rig_factory: RigFactory, store: Store, plan: CalibrationPlan, *,
                    now: datetime | None = None, out_dir: str | Path | None = None,
                    holidays: Iterable[date] = (), params: Mapping[str, Any] | None = None,
                    mutant_crops: Mapping[str, Mapping[str, Sequence[Any]]] | None = None) -> CalibrationResult:
    """Run one Calibration run (module docstring). `cfg` is the run's `pipeline.config.SentinelConfig` (masks,
    corpus hashes, fixture epoch, regions files, flow and surface owners); its rigs and build must be registered in
    `store`. `mutant_crops` maps a checkpoint to its regions' canary mutant crops (9.7 fault separation); `out_dir`
    gets `calibration/<hex>.yaml` per rendering key."""
    p = params or load_params()
    n1, n2 = int(p["calibration"]["session_captures"]), int(p["calibration"]["next_session_captures"])
    now = _aware(now or plan.second_night)
    shards = plan.shards
    shard_ids = {(s.rig_id, night): f"{plan.run_id}/{(night - 1) * len(shards) + k}"
                 for night in (1, 2) for k, s in enumerate(shards, 1)}
    store.register_checkpoints([ScopeEntry(i.checkpoint.checkpoint_id, i.checkpoint.workspace_id, i.checkpoint.persona,
                                           i.checkpoint.tier, i.checkpoint.data_mode,
                                           flow_class=_flow_class(cfg, i.checkpoint)) for i in plan.items],
                               config_commit=plan.config_commit)
    store.begin_run(RunPlan(
        run_id=plan.run_id, run_type="calibration", config_commit=plan.config_commit,
        config_tarball_sha256=plan.config_tarball_sha256, snapshot_set_sha256=plan.snapshot_set_sha256,
        harness_sha=plan.harness_sha, authorization_ref=plan.authorization_ref,
        budget_sec=int((_aware(plan.window_end) - _aware(plan.window_start)).total_seconds()), scope=(),
        shards=tuple(ShardPlan(shard_ids[(s.rig_id, night)], s.rig_id, sides="candidate", scope_count=0)
                     for night in (1, 2) for s in shards),
        started_at=_aware(plan.window_start).isoformat()))

    shots: dict[str, list[Shot | None]] = {i.checkpoint.checkpoint_id: [None] * (n1 + n2) for i in plan.items}
    registries: dict[str, RegionsFile] = {}
    reports: dict[tuple[str, int], SessionReport] = {}
    sessions: list[dict] = []
    env_shards = False                                    # a shard raised to complete_with_env_errors (choice 6)
    for night, count, offset in ((1, n1, 0), (2, n2, n1)):
        for shard in shards:
            sid = shard_ids[(shard.rig_id, night)]

            def work(ctx: SessionContext, shard: CalibrationShard = shard, count: int = count,
                     offset: int = offset) -> None:
                for k in range(count):
                    for item in shard.items:
                        cid = item.checkpoint.checkpoint_id
                        shots[cid][offset + k] = ctx.capture(item, offset + k)
                        if cid not in registries:
                            registries[cid] = ctx.registry(item)

            rep = rig_factory(shard, night, sid).run(work)
            reports[(shard.rig_id, night)] = rep
            if rep.fingerprint is not None:
                store.write_fingerprint(rep.fingerprint)
            for item in shard.items:
                row = shots[item.checkpoint.checkpoint_id]
                for k in range(offset, offset + count):
                    row[k] = row[k] or _failed(rep.stopped)
            key = _key(rep)
            env = any(_capture(s).outcome == ENV_ERRORS[1] for item in shard.items
                      for s in shots[item.checkpoint.checkpoint_id][offset:offset + count] if s is not None)
            env = env or (night == 2 and _key_changed(reports[(shard.rig_id, 1)], rep))
            status, reason = _raised(rep.status, rep.reason, env)
            env_shards |= status != rep.status
            store.end_shard(sid, status, reason)
            store.mark_shard_imported(sid)
            sessions.append({"shard_id": sid, "rig": shard.rig_id, "night": night, "status": status,
                             "reason": reason, "last_gate": rep.last_gate, "calibration_key": key,
                             "started_at": rep.started_at.isoformat() if rep.started_at else None})

    calibrations: dict[str, CheckpointCalibration] = {}
    keys: dict[str, str | None] = {}
    by_key: dict[str | None, list[CheckpointCalibration]] = {}
    live: dict[str, LiveVersions] = {}
    fingerprints: dict[str | None, str | None] = {}
    for shard in shards:
        r1, r2 = reports[(shard.rig_id, 1)], reports[(shard.rig_id, 2)]
        k1 = _key(r1)
        fingerprints.setdefault(k1, r1.fingerprint.fingerprint_id if r1.fingerprint is not None else None)
        for item in shard.items:
            cp = item.checkpoint
            row = [s for s in shots[cp.checkpoint_id] if s is not None]
            if _key_changed(r1, r2):
                row[n1:] = [Shot(None, *KEY_CHANGED) for _ in row[n1:]]
            reg = registries.get(cp.checkpoint_id, RegionsFile(cp.checkpoint_id))
            boxes = tuple(dict.fromkeys(b for s in row for b in s.redactions))
            park = next((s.park for s in row if s.park is not None), None)
            cal = calibrate_checkpoint([_capture(s) for s in row], reg, data_mode=cp.data_mode, redactions=boxes,
                                       system_masks=[park_rect(park)] if park is not None else [], now=now,
                                       params=p, park=park, tier=cp.tier, owner=item.owner,
                                       mutant_crops=(mutant_crops or {}).get(cp.checkpoint_id))
            calibrations[cp.checkpoint_id], keys[cp.checkpoint_id] = cal, k1
            by_key.setdefault(k1, []).append(cal)
            live[cp.checkpoint_id] = versions_for(cfg, cp, reg, r1.redaction_version)

    exceptions = live_exceptions(cfg.regions, now)
    files: list[Path] = []
    open_items: list[FixItem] = []
    for key, cals in by_key.items():
        if key is None:                                    # choice 3: no rendering key, fix items only
            store.write_calibration([], [r for c in cals for r in fix_rows(c, run_id=plan.run_id)])
            open_items += [i for c in cals for i in c.fix_list if not i.closed]
            continue
        write_calibration(store, cals, calibration_key=key, fingerprint_id=fingerprints.get(key), run_id=plan.run_id,
                          now=now, live=live)
        open_items += StoreCalibration(store, key, exceptions=exceptions).fix_list()
        exported = export_file(store, key, exported_at=now, exceptions=exceptions)
        if out_dir is not None and exported is not None:
            files.append(exported.save(Path(out_dir) / "calibration"))

    hol = tuple(holidays)
    routes: dict[str, list[str]] = {}
    for item in open_items:
        routes.setdefault(item.owner or "unassigned", []).append(item.text(now, hol))
    worst = _worst(list(reports.values()))
    status, reason = _raised(worst.status, worst.reason, env_shards)
    summary = {"calibration": {
        "build_id": plan.build_id, "checkpoints": len(calibrations),
        "calibrated": sum(1 for c in calibrations.values() if not c.failed),
        "keys": sorted({k for k in keys.values() if k is not None}), "sessions": sessions,
        "fix_list": fix_list_lines(open_items, now, hol), "fix_routes": routes,
        "overdue": [i.label() for i in overdue(open_items, now, hol)],
        "files": [f.name for f in files]}}
    outcome = store.end_run(RunOutcome(plan.run_id, "calibration", status, worst.last_gate, reason=reason,
                                       started_at=_aware(plan.window_start).isoformat(), ended_at=now.isoformat()),
                            summary=summary)
    return CalibrationResult(outcome, calibrations, keys, open_items, files, sessions, summary["calibration"])


# ------------------------------------------------------------------------------------------------
# The simulated rig (`sentinel calibrate`)

class SimRigs:
    """A `RigFactory` over the simulated rig: per shard and night a session through the Orchestrator's gates on
    `orchestrator.simrig.SimulatedRig` (`pipeline.orchestration.shard_setup`), its fingerprint from the rig's reads
    (`fingerprint.build_fingerprint`, the PR run's harness fields), captures from `pipeline.sim.SimApp` of the build,
    settled by `diff.settle.settle` and redacted by `capture.redact_pair` like a product run's (3.9 step 2)."""

    def __init__(self, cfg: Any, build: Any, plan: CalibrationPlan, *, params: Mapping[str, Any] | None = None):
        from .capture import RedactionList, Sweep, redaction_version
        from .pipeline.config import tree_sha256
        from .pipeline.sim import SimApp

        self.cfg, self.plan, self.params = cfg, plan, params or load_params()
        self.app = SimApp(build)
        anchors = cfg.root / "redaction" / "anchors.yaml"
        self.anchors = RedactionList.load(anchors, cfg.root) if anchors.is_file() else RedactionList()
        ids = tuple(str(a["account_id"]) for a in cfg.fixture.get("accounts") or () if a.get("account_id"))
        self.sweep = Sweep(ids, display_name=cfg.fixture.get("display_name"))
        self.redaction_version = redaction_version(self.anchors, self.sweep)
        self.tree = tree_sha256(cfg.root)
        self.registries: dict[str, RegionsFile] = {}

    def __call__(self, shard: CalibrationShard, night: int, shard_id: str) -> "_SimSession":
        return _SimSession(self, shard, night, shard_id)

    def registry(self, item: CalibrationItem, screen: Any, info: Any) -> RegionsFile:
        """The checkpoint's effective regions file on the simulated screen (the PR run's `_Run.registry`)."""
        from .diff.regions import effective_registry
        from .pipeline.sim import SimApp

        cp = item.checkpoint
        if cp.checkpoint_id not in self.registries:
            host = dict(cp.size_step or {}).get("host")
            doc = self.cfg.regions_doc(cp.checkpoint_id, cp.surface_id, cp.test_kind, str(host) if host else None)
            reg, _ = effective_registry(SimApp.registry(cp, screen, info, doc), self.cfg.masks,
                                        _aware(self.plan.window_start))
            self.registries[cp.checkpoint_id] = reg
        return self.registries[cp.checkpoint_id]


class _SimSession:
    """One simulated session (a `CalibrationSession`)."""

    def __init__(self, rigs: SimRigs, shard: CalibrationShard, night: int, shard_id: str):
        self.rigs, self.shard, self.night, self.shard_id = rigs, shard, night, shard_id
        self.shots: dict[str, Any] = {}

    def run(self, work: Callable[[SessionContext], None]) -> SessionReport:
        from .fingerprint import build_fingerprint, harness_block
        from .orchestrator.budgets import WallBudget
        from .orchestrator.clock import ManualClock
        from .orchestrator.identity import account_id_sha256
        from .orchestrator.machine import Orchestrator
        from .orchestrator.results import ControlResult
        from .orchestrator.simrig import SimulatedRig
        from .pipeline.orchestration import RigScript, ShardSink, login_alias, shard_setup

        rigs, plan, shard = self.rigs, self.rigs.plan, self.shard
        start = _aware(plan.window_start if self.night == 1 else plan.second_night)
        shard_no = int(self.shard_id.rsplit("/", 1)[1])
        clock = ManualClock(start=start.astimezone(timezone.utc), mono=1000.0 + 10_000 * shard_no)
        workspace = shard.items[0].checkpoint.workspace_id if shard.items else "ws.sim"
        ctx, sim = shard_setup(rigs.cfg, run_id=plan.run_id, run_type="calibration", shard_id=self.shard_id,
                               shard_no=shard_no, build_id=plan.build_id, workspace_id=workspace, script=RigScript(),
                               rig_id=shard.rig_id)
        rig = SimulatedRig(sim, clock=clock)
        sink = ShardSink([i.checkpoint.checkpoint_id for i in shard.items])
        orch = Orchestrator(rig, ctx, sink, clock=clock, wall=WallBudget.for_run(
            "calibration", clock.monotonic(), started_at=start, ends_at=_aware(plan.window_end)))
        recorded: list[Fingerprint] = []

        def go(o: Orchestrator) -> None:
            harness = harness_block(
                version="sentinel-core-0.1.0-sim", ocr_engine="sim-ocr-1", ocr_model="synth-drawn-text",
                capture_lib="sim-capture-1", redaction_version=rigs.redaction_version,
                region_registry_version=rigs.tree[:12], config_commit=rigs.tree[:12],
                config_tarball_sha256="sha256:" + rigs.tree, allowlist_sha256="sha256:" + ctx.allowlist.sha256,
                diff_params=rigs.params)
            epoch = str(((rigs.cfg.fixture.get("epoch") or {}).get("epoch_id")) or "epoch-unknown")
            fp = build_fingerprint(rig.read_os(), rig.read_app(), plan.run_id, self.shard_id, "candidate",
                                   shard.rig_id, harness, data_epoch={"epoch_id": epoch, "data_mode": "backend_fixture"},
                                   account={"account_id_sha256": account_id_sha256(ctx.fixture_account),
                                            "login_sha256": account_id_sha256(login_alias(shard.rig_id)),
                                            "role": "empty"},
                                   read_at=clock.now().isoformat())
            if isinstance(fp, ControlResult):
                o.apply(fp)
                return
            recorded.append(fp)
            work(_SimContext(rigs, plan.run_id))

        report = orch.run(go)
        filled = next(iter(sink.filled.values()), None)
        return SessionReport(report.verdict.status, report.last_gate, report.verdict.reason,
                             recorded[0] if recorded else None,
                             (filled.outcome, filled.reason) if filled is not None else None,
                             rigs.redaction_version, start)


class _SimContext:
    """The simulated session in `running` (a `SessionContext`)."""

    def __init__(self, rigs: SimRigs, run_id: str):
        self.rigs, self.run_id = rigs, run_id
        self.screens: dict[str, tuple[Any, Any]] = {}

    def capture(self, item: CalibrationItem, index: int) -> Shot:
        from .capture import RedactionContext, SyntheticOcr, redact_pair
        from .diff.settle import settle
        from .evaluation import settle_spec_for
        from .synth import THEME

        rigs, cp = self.rigs, item.checkpoint
        cap = rigs.app.capture(cp, "base", controls=rigs.cfg.control_ids(cp.surface_id), tick=10 + index)
        self.screens.setdefault(cp.checkpoint_id, (cap.screen, cap.info))
        reg = self.registry(item)
        h, w = cap.screen.image.shape[:2]
        park = (w - 8, h - 8)                           # the PR run's parking coordinate (`pipeline.run._park`)
        spec = settle_spec_for(rigs.cfg, cp, regfile=reg, park=park, template=lambda i, p: None, params=rigs.params)
        s = settle(cap.frames, spec, rigs.params)
        if not s.settled or s.capture is None:
            return Shot(None, "not_settled", s.reason, s.time_to_settle_ms, park=park)
        ocr = SyntheticOcr.from_screen(cap.screen)
        red = redact_pair(s.capture, s.capture, rigs.anchors, {"base": ocr, "candidate": ocr}, THEME["bg"],
                          sweep=rigs.sweep, context=RedactionContext(self.run_id, cp.checkpoint_id, cp.tier),
                          params=rigs.params)
        if red.terminal is not None or red.candidate is None:
            t = red.terminal
            return Shot(None, t.outcome if t else "harness_error", t.reason if t else "redaction_version_missing",
                        s.time_to_settle_ms, park=park)
        return Shot(red.candidate, time_to_settle_ms=s.time_to_settle_ms, redactions=tuple(red.boxes), park=park)

    def registry(self, item: CalibrationItem) -> RegionsFile:
        screen, info = self.screens[item.checkpoint.checkpoint_id]
        return self.rigs.registry(item, screen, info)


# ------------------------------------------------------------------------------------------------
# `sentinel calibrate`

def _register(store: Store, cfg: Any, plan: CalibrationPlan) -> str | None:
    """Register the plan's rigs and build (8.3 `rigs`, `builds`); the problem when the build has no record."""
    from .pipeline.orchestration import login_alias

    for n, shard in enumerate(plan.shards, 1):
        store.register_rig(shard.rig_id, login_alias=login_alias(shard.rig_id), owner="sentinel",
                           snapshot_id=f"snap-{shard.rig_id}", allowlist_entry=n)
    doc = cfg.builds.get(plan.build_id)
    if doc is None:
        return f"build {plan.build_id} has no record under builds/ (4.2)"
    b = doc["build"]
    store.register_build(plan.build_id, channel=b["channel"], source_commit=b["commit_sha"],
                         pipeline_artifact_url=(b.get("artifact") or {}).get("uri", "sim://"),
                         installer_sha256=str((b.get("artifact") or {}).get("sha256", "sim")).split(":")[-1],
                         branch=b.get("branch"), first_seen_at=str(b["built_at"]) if b.get("built_at") else None)
    return None


def main(argv: Sequence[str] | None = None, *, rig_factory: RigFactory | None = None) -> int:
    """`sentinel calibrate CONFIG_DIR PLAN_JSON --out DIR [--db PATH]`: one Calibration run over the plan on the
    simulated rig (or `rig_factory`), into the store `DIR/sentinel.db` (the PR runs' store, unless `--db`), with
    `calibration/<hex>.yaml` under DIR. Exit 0 when the run is `complete`, 1 when it is not, 2 on a usage error."""
    from .pipeline.config import SentinelConfig, tree_sha256
    from .pipeline.sim import SimBuild

    ap = argparse.ArgumentParser(prog="sentinel calibrate", description="Run a Calibration run (3.9).")
    ap.add_argument("config_dir")
    ap.add_argument("plan", help="the plan file (JSON, CalibrationPlan.to_dict)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--db", default=None)
    args = ap.parse_args(list(argv) if argv is not None else None)
    root, plan_path = Path(args.config_dir), Path(args.plan)
    if not root.is_dir() or not plan_path.is_file():
        print(f"sentinel calibrate: {root if not root.is_dir() else plan_path} not found", file=sys.stderr)
        return 2
    try:
        plan = CalibrationPlan.from_dict(json.loads(plan_path.read_text(encoding="utf-8")))
    except (ValueError, KeyError, TypeError) as e:
        print(f"sentinel calibrate: bad plan: {e}", file=sys.stderr)
        return 2
    cfg = SentinelConfig.load(root)
    if plan.config_commit == "unknown":
        sha = tree_sha256(root)
        plan = CalibrationPlan(plan.run_id, plan.build_id, plan.shards, plan.window_start, plan.window_end,
                               plan.second_night, sha[:12], sha, hashlib.sha256(
                                   ",".join(s.rig_id for s in plan.shards).encode()).hexdigest(),
                               "sentinel-core-0.1.0-sim", plan.authorization_ref)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with Store(Path(args.db) if args.db else out / "sentinel.db") as store:
        problem = _register(store, cfg, plan)
        if problem:
            print(f"sentinel calibrate: {problem}", file=sys.stderr)
            return 2
        result = run_calibration(cfg, rig_factory or SimRigs(cfg, SimBuild(plan.build_id), plan), store, plan,
                                 out_dir=out)
    print(f"calibration run {plan.run_id}: {result.outcome.status}; {len(result.calibrations)} checkpoints, "
          f"{len(result.fix_list)} open fix item(s); files: {', '.join(f.name for f in result.files) or 'none'}")
    for line in result.summary.get("fix_list", []):
        print(f"  {line}")
    return 0 if result.outcome.status == "complete" else 1
