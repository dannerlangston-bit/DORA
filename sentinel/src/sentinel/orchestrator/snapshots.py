"""The snapshot lifecycle over the hypervisor port: names, revert with retry, create, validate, retire and the pair's
revert plan (7.2, 7.3; 12.7 R1).

- Names (7.2): `base-os.<gen>`, `app.<build_id>.<gen>`, `explorer.<build_id>.<gen>` and `upgrade.<build_id>.<gen>`
  (the previous released builds N-1 and N-2); `parse_snapshot_name` / `snapshot_name`, and `select_snapshot` (DR-04
  Branch B: the snapshot is chosen by build id; a quarantined one never is).
- `revert_with_retry(hypervisor, snapshot_id, clock, ...)`: a failed revert (False, an API error or a timeout) is
  retried once after 60 s; a second failure is `env_error (gate: provisioned, reason: snapshot_revert_failed)` on every
  checkpoint of the shard and the run `complete_with_env_errors`, or, when the budget allows and a pool rig of the same
  base-os generation is free (`rigmanager.RigPool.requeue_target`), a re-queue of the shard to that rig.
- `r1_snapshot_actions(r1)`: a revert that reports success onto a wrong or tampered snapshot fails preflight R1
  (`env_error (gate: preflight.R1)`, decided by `gates.check_provisioned`); the snapshot is then quarantined and
  rebuilt.
- `create_snapshot(hypervisor, name, steps, ...)`: the 7.3 steps in order; a step that raises is `install_failed` with
  the installer log (`rigmanager.install_failed`), a step that returns its own failure (POP-03's agreement_pending)
  keeps it, and no snapshot is written either way.
- `validate_snapshot(...)`: the live fingerprint equals the manifest written at creation and one smoke flow passed;
  otherwise the snapshot is deleted, a maintenance item opens for the Rig Manager owner and runs that need the build
  take `install_failed`.
- `retention_plan(names, builds, in_flight=...)`: which snapshots retire (7.2: a build older than the last release and
  not one of the two newest main builds; disk per rig holds those, the last release, the two upgrade snapshots and the
  builds of runs in flight). Fingerprint and calibration rows are never touched: they live in SQLite.
- `pair_revert_plan(base, candidate, base_reused=...)`: two reverts for a pair on one rig, the base half's snapshot
  first, then the candidate's; one when the base capture is reused. `branch_b_pair_problem` checks the Branch B rule
  that both halves revert to snapshots taken the same night.
- `SimulatedHypervisor`: a scriptable `SnapshotHypervisor` for tests and the simulated rig.

Choices where the spec is ambiguous or silent (reported):

1. The hypervisor port of `ports.Hypervisor` has no delete; `SnapshotHypervisor` adds `delete(snapshot_id) -> bool` for
   validate and retire. A snapshot's id is its name.
2. `results.ACTIONS` has no snapshot verbs, so the lifecycle's own steps travel beside the control result as
   `LifecycleResult.snapshot_actions` (`quarantine_snapshot`, `rebuild_snapshot`, `delete_snapshot`) with the
   maintenance item; the shard's re-queue is the existing `requeue_shard` action. On a re-queue no checkpoint outcome
   is written here: the re-queued shard decides them, as for a lost rig (`degraded.rig_lost`).
3. R1 quarantines the snapshot when the manifest hash or the Windows build differs from the pin (a wrong or tampered
   snapshot); a pending restart alone fails R1 without a quarantine.
4. Retention keeps every `base-os` generation (7.2 retires builds, not generations), every snapshot of a build that is
   not in `builds` (nothing is deleted on a guess), and the builds named by runs in flight on either side (a PR run's
   base can be an older main build than the two newest).
5. "The same night" (Branch B) is the America/New_York date of the snapshot time minus 12 hours, so 22:00 and 01:00
   belong to one night.
6. The 7.2 table's "pair on one rig is two reverts" orders the base half first, as 7.2 writes it (the gap entry said
   candidate first; the spec text wins).
7. The upgrade snapshots kept are those of the last two releases: the release-night upgrade run starts from the
   "N-1 snapshot" at every RC cut and at G-1 (11.8 DR-04 Branch B, 10.9 G-1 "from the previous released build"),
   where N is the RC, so N-1 is the last release and N-2 the one before; 7.2's retire rule never retires the last
   release. An upgrade snapshot of an older release is retired unless a run in flight names its build.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from ..lint.rules import fingerprint_differences
from ..records import MaintenanceItem
from ..rigmanager import install_failed
from . import alerts
from .clock import Clock
from .ports import Hypervisor
from .results import ControlResult, Outcome, RunVerdict

KINDS = ("base-os", "app", "explorer", "upgrade")
REVERT_RETRY_S = 60.0                        # 7.2
NEW_YORK = ZoneInfo("America/New_York")
NIGHT_SHIFT = timedelta(hours=12)            # choice 5
SNAPSHOT_ACTIONS = ("quarantine_snapshot", "rebuild_snapshot", "delete_snapshot")
VALIDATE_MAINTENANCE_KIND = "snapshot_validate_failed"


# -- names ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SnapshotName:
    """A parsed snapshot name (7.2)."""

    kind: str
    gen: str
    build_id: str | None = None

    def __str__(self) -> str:
        return snapshot_name(self.kind, self.gen, self.build_id)


def snapshot_name(kind: str, gen: str, build_id: str | None = None) -> str:
    """`base-os.<gen>` or `<kind>.<build_id>.<gen>` (7.2)."""
    if kind not in KINDS or not gen or "." in gen:
        raise ValueError(f"snapshot kind {kind!r} and generation {gen!r} (7.2)")
    if kind == "base-os":
        if build_id is not None:
            raise ValueError("a base-os snapshot holds no build")
        return f"base-os.{gen}"
    if not build_id:
        raise ValueError(f"a {kind} snapshot names its build")
    return f"{kind}.{build_id}.{gen}"


def parse_snapshot_name(name: str) -> SnapshotName:
    """The kind, build and generation of a snapshot name; the build id may hold dots, the generation never does."""
    kind, _, rest = name.partition(".")
    if kind not in KINDS or not rest:
        raise ValueError(f"{name!r} is not a 7.2 snapshot name")
    if kind == "base-os":
        if "." in rest:
            raise ValueError(f"{name!r}: base-os.<gen> only")
        return SnapshotName(kind, rest)
    build, _, gen = rest.rpartition(".")
    if not build or not gen:
        raise ValueError(f"{name!r}: {kind}.<build_id>.<gen>")
    return SnapshotName(kind, gen, build)


def select_snapshot(names: Iterable[str], *, kind: str, build_id: str | None, gen: str,
                    quarantined: Iterable[str] = ()) -> str | None:
    """The snapshot of `kind` for a build and generation (Branch B: selection by build id, 5.3), skipping quarantined
    ones; None when the rig has none yet (the Rig Manager creates it the first time the rig needs the build)."""
    want = snapshot_name(kind, gen, build_id)
    return want if want in set(names) and want not in set(quarantined) else None


# -- the port and its simulation ----------------------------------------------------------------------------------
@runtime_checkable
class SnapshotHypervisor(Hypervisor, Protocol):
    """`ports.Hypervisor` plus delete (choice 1)."""

    def delete(self, snapshot_id: str) -> bool:
        """Delete a snapshot; False when it could not be deleted."""


class SimulatedHypervisor:
    """A deterministic `SnapshotHypervisor`: snapshots by name, `revert_failures` scripted failed reverts (raised as
    TimeoutError when `raise_on_failure`), and a call log."""

    def __init__(self, snapshots: Iterable[str] = (), *, revert_failures: int = 0, raise_on_failure: bool = False):
        self._snapshots = list(dict.fromkeys(snapshots))
        self.revert_failures = revert_failures
        self.raise_on_failure = raise_on_failure
        self.current: str | None = None
        self.log: list[tuple[str, str]] = []

    def revert(self, snapshot_id: str) -> bool:
        self.log.append(("revert", snapshot_id))
        if self.revert_failures > 0:
            self.revert_failures -= 1
            if self.raise_on_failure:
                raise TimeoutError(f"hypervisor timed out reverting {snapshot_id}")
            return False
        if snapshot_id not in self._snapshots:
            return False
        self.current = snapshot_id
        return True

    def snapshot(self, name: str) -> str:
        self.log.append(("snapshot", name))
        if name not in self._snapshots:
            self._snapshots.append(name)
        return name

    def snapshots(self) -> tuple[str, ...]:
        return tuple(self._snapshots)

    def delete(self, snapshot_id: str) -> bool:
        self.log.append(("delete", snapshot_id))
        if snapshot_id not in self._snapshots:
            return False
        self._snapshots.remove(snapshot_id)
        return True


@dataclass(frozen=True)
class LifecycleResult:
    """A lifecycle step's control result with the snapshot steps it asks for (choice 2)."""

    control: ControlResult
    snapshot_actions: tuple[str, ...] = ()
    snapshot_id: str | None = None
    maintenance: MaintenanceItem | None = None

    def __post_init__(self) -> None:
        bad = set(self.snapshot_actions) - set(SNAPSHOT_ACTIONS)
        if bad:
            raise ValueError(f"unknown snapshot actions {sorted(bad)}")


# -- revert (7.2) -------------------------------------------------------------------------------------------------
def _try_revert(hv: Hypervisor, snapshot_id: str) -> str | None:
    """None when the revert succeeded, else why it failed. Any error the hypervisor client raises is a failed revert
    (7.2: "hypervisor API error or timeout")."""
    try:
        return None if hv.revert(snapshot_id) else "the hypervisor reported the revert failed"
    except Exception as e:  # noqa: BLE001 - the port boundary: every client error is a failed revert
        return f"{type(e).__name__}: {e}"


def revert_with_retry(hv: Hypervisor, snapshot_id: str, clock: Clock, *, requeue_rig: str | None = None,
                      budget_allows: bool = False) -> ControlResult:
    """Revert, retrying once after 60 s (7.2). Passed with the attempt count; after two failures a re-queue to
    `requeue_rig` when the budget allows (choice 2), else `snapshot_revert_failed` on every checkpoint of the shard."""
    first = _try_revert(hv, snapshot_id)
    if first is None:
        return ControlResult("snapshot_revert", True, "provisioned", evidence={"snapshot_id": snapshot_id,
                                                                               "attempts": 1})
    clock.sleep(REVERT_RETRY_S)
    second = _try_revert(hv, snapshot_id)
    ev = {"snapshot_id": snapshot_id, "attempts": 2, "failures": [first] + ([second] if second else [])}
    if second is None:
        return ControlResult("snapshot_revert", True, "provisioned", detail=f"revert retried after 60 s: {first}",
                             evidence=ev)
    detail = f"revert of {snapshot_id} failed twice, 60 s apart: {second}"
    if budget_allows and requeue_rig is not None:
        return ControlResult("snapshot_revert", False, "provisioned", actions=("requeue_shard",),
                             alert=alerts.env_error(), detail=f"{detail}; the shard is re-queued to {requeue_rig}",
                             evidence={**ev, "requeue_to": requeue_rig})
    return ControlResult("snapshot_revert", False, "provisioned", run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", "snapshot_revert_failed", "provisioned", detail),
                         alert=alerts.env_error(), detail=detail, evidence=ev)


def r1_snapshot_actions(causes: Sequence[str]) -> tuple[str, ...]:
    """The snapshot steps a failed R1 asks for (7.2, choice 3): quarantine and rebuild when the manifest hash or the
    Windows build differs (`orchestrator.preflight` R1 causes start `manifest:` or `windows_build:`)."""
    wrong = any(c.startswith(("manifest:", "windows_build:")) for c in causes)
    return ("quarantine_snapshot", "rebuild_snapshot") if wrong else ()


# -- create and validate (7.2, 7.3) -------------------------------------------------------------------------------
Step = tuple[str, Callable[[], ControlResult | None]]


def create_snapshot(hv: Hypervisor, name: str, steps: Sequence[Step], *,
                    installer_log_sha256: str | None = None) -> LifecycleResult:
    """Run the creation steps in order and take the snapshot (7.3 steps 2 to 7); no snapshot is written when a step
    fails (7.2: install, vmoptions write, corpus copy, first launch, fingerprint read)."""
    parse_snapshot_name(name)
    for step, run in steps:
        try:
            r = run()
        except Exception as e:  # noqa: BLE001 - any failure of a creation step is install_failed (7.2)
            r = install_failed(step, f"{type(e).__name__}: {e}", installer_log_sha256)
        if r is not None and not r.passed:
            return LifecycleResult(r)
    sid = hv.snapshot(name)
    return LifecycleResult(ControlResult("snapshot_create", True, "provisioned", evidence={"snapshot_id": sid}),
                           snapshot_id=sid)


def validate_snapshot(hv: SnapshotHypervisor, snapshot_id: str, *, manifest: Mapping[str, Any],
                      live: Mapping[str, Any], smoke_passed: bool, owner: str, at: str) -> LifecycleResult | None:
    """7.2 validate: None when the live fingerprint's `match` equals the manifest's (build id aside) and the smoke flow
    passed; otherwise delete the snapshot, open a maintenance item for the Rig Manager owner and give the runs that
    need the build `install_failed`."""
    differing = fingerprint_differences(manifest, live)
    if not differing and smoke_passed:
        return None
    why = ([f"fingerprint differs from the manifest: {', '.join(differing)}"] if differing else []) + \
          ([] if smoke_passed else ["the smoke flow failed"])
    detail = "; ".join(why)
    if not hv.delete(snapshot_id):
        detail += "; the snapshot could not be deleted"
    item = MaintenanceItem(kind=VALIDATE_MAINTENANCE_KIND, ref=snapshot_id, owner=owner, opened_at=at, detail=detail)
    return LifecycleResult(install_failed("validate", detail, None), ("delete_snapshot",), snapshot_id, item)


# -- retire (7.2) -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RetentionPlan:
    """What retires on one rig and why each kept snapshot stays (7.2)."""

    delete: tuple[str, ...]
    keep: Mapping[str, str] = field(default_factory=dict)


def _ts(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def retention_plan(names: Iterable[str], builds: Mapping[str, Mapping[str, Any]], *,
                   in_flight: Iterable[str] = ()) -> RetentionPlan:
    """The 7.2 retire rule over a rig's snapshots (choice 4). `builds` maps build id to its 4.2 record fields
    (`channel`, `built_at`, `released_at`); `in_flight` names the builds of runs not yet reported."""
    released = sorted((b for b, r in builds.items() if r.get("channel") == "released"),
                      key=lambda b: _ts(builds[b].get("released_at") or builds[b]["built_at"]), reverse=True)
    mains = sorted((b for b, r in builds.items() if r.get("channel") == "main"),
                   key=lambda b: _ts(builds[b]["built_at"]), reverse=True)
    last_release = released[0] if released else None
    upgrades = set(released[:2])                     # N-1 and N-2 of the next release (choice 7)
    flying = set(in_flight)
    keep: dict[str, str] = {}
    delete: list[str] = []
    for name in sorted(set(names)):
        s = parse_snapshot_name(name)
        b = s.build_id
        if s.kind == "base-os":
            keep[name] = "base-os generation"
        elif b not in builds:
            keep[name] = "build not in the records: never deleted on a guess"
        elif s.kind == "upgrade":
            if b in upgrades:
                keep[name] = "upgrade snapshot of release N-1 or N-2"
            elif b in flying:
                keep[name] = "a build of a run in flight"
            else:
                delete.append(name)
        elif b == last_release:
            keep[name] = "the last released build"
        elif b in mains[:2]:
            keep[name] = "one of the two newest main builds"
        elif b in flying:
            keep[name] = "a build of a run in flight"
        else:
            delete.append(name)
    return RetentionPlan(tuple(delete), keep)


# -- pairs (7.2) --------------------------------------------------------------------------------------------------
def pair_revert_plan(*, base: str | None, candidate: str, base_reused: bool) -> tuple[str, ...]:
    """The reverts of a pair on one rig (7.2, choice 6): the base half's snapshot, then the candidate's; only the
    candidate's when the base capture is reused."""
    if base_reused:
        return (candidate,)
    if base is None:
        raise ValueError("a pair that does not reuse its base reverts the base snapshot first (7.2)")
    return (base, candidate)


def night_of(taken_at: datetime) -> str:
    """The night a snapshot was taken (choice 5)."""
    if taken_at.tzinfo is None:
        raise ValueError("a snapshot time is an aware time")
    return (taken_at.astimezone(NEW_YORK) - NIGHT_SHIFT).date().isoformat()


def branch_b_pair_problem(base_taken_at: datetime, candidate_taken_at: datetime) -> str | None:
    """Branch B (7.2): both halves revert to snapshots taken the same night; None when they were, else the problem
    (the older half is retaken after tonight's updater run)."""
    a, b = night_of(base_taken_at), night_of(candidate_taken_at)
    return None if a == b else f"Branch B pair snapshots from different nights: base {a}, candidate {b} (7.2)"
