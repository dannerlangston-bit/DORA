"""The Rig Manager's platform-independent logic (5.3, 5.14, 7.1, 7.3, 10.7, 11.2).

- vmoptions (7.3 step 4, 5.3 "vmoptions re-applied and verified by hash", 5.14, 12.7 B3 and H10):
  `render_vmoptions(template, values)` fills the checked-in template's `${name}` placeholders with the rig cell's
  pinned values (the bytes written and hashed); `verify_vmoptions(text, pinned_sha256=..., rig_kind=..., jdk_major=...)`
  lists what is wrong with a written file (hash, crash flags, Java2D flags, flags unknown to the JDK major);
  `vmoptions_failure(problems, at_creation=...)` is the outcome: `install_failed` while a snapshot is being created,
  `env_error (gate: preflight.B3)` on a re-apply before a run.
- Install (7.3): `INSTALL_STEPS` per DR-04 branch, `install_outcome` (a non-zero installer exit or a missing main
  executable is `install_failed` with the installer log) and `first_launch_agreement` (POP-03 at the first launch stops
  the build as `env_error (gate: authenticated, reason: agreement_pending)` with the operator logged).
- `RigPool` (5.3, 7.1, 11.2): which login is on which rig, the spare substitution (rig 6 replaces a failed pool rig
  only by being re-snapshotted with the failed rig's login, the operator recorded in run health, no Explorer session
  while it serves the pool) and the re-queue target for a shard (a free pool rig of the same base-os generation, 7.2
  and 10.7).
- Rig heartbeat (5.3, 7.11, 10.7): `RigHeartbeat`, the `HeartbeatSender` port, `HeartbeatPacer` (every 30 s) and
  `HeartbeatMonitor` (3 minutes of silence is a lost rig, `orchestrator.degraded.rig_silent`).

Choices where the spec is ambiguous or silent (reported):

1. The template's placeholders are `string.Template` names (`${heap_max}`); every placeholder needs a value and every
   value a placeholder, so a typo cannot render a file that silently lacks a setting. The rendered text is written
   byte for byte, and its sha256 is the pinned `vmoptions_hash` B3 and H10 compare (rigfiles choice 1).
2. A spare substitution re-snapshots the spare with the failed rig's login (a new snapshot id, as
   `store.Store.reassign_login` requires), deactivates the failed rig and gives the spare its role; the Explorer is
   disabled until `release_spare` gives rig 6 its Explorer login back. Every change is a `PoolEvent` with the
   operator and time for run health; the store's `reassign_login` records the same move in `rigs`.
3. The re-queue target is an active pool rig other than the failed one, of the same base-os generation, not busy; the
   lowest rig id wins so the choice is deterministic. A lost rig's "same fingerprint class" (5.3) is read as the same
   generation too, since the generation fixes the Windows build, display and hardening of the fingerprint.
4. The pacer sends the first heartbeat at once and the next 30 s after the previous one; the monitor counts silence
   from the last heartbeat received, or from when it started watching a rig that never sent one.
"""
from __future__ import annotations

import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from .orchestrator import alerts
from .orchestrator.clock import Clock
from .orchestrator.degraded import rig_silent
from .orchestrator.results import ControlResult, Outcome, RunVerdict
from .rigfiles import validate_vmoptions, vmoptions_sha256

HEARTBEAT_INTERVAL_S = 30.0          # 5.3
SPARE_RIG = "rig-06"                 # 7.1: rig 6 is the Explorer rig and the pool spare
ROLES = ("pool", "nightly", "explorer")


# -- vmoptions --------------------------------------------------------------------------------------------------
def placeholders(template: str) -> frozenset[str]:
    """The `${name}` placeholders of a vmoptions template (choice 1); ValueError at a `$` that starts no placeholder
    (write `$$` for a literal dollar sign)."""
    names = set()
    for m in string.Template.pattern.finditer(template):
        if m.group("invalid") is not None:
            line = template.count("\n", 0, m.start()) + 1
            raise ValueError(f"line {line}: a '$' that starts no placeholder (write $$ for a literal one)")
        if m.group("named") or m.group("braced"):
            names.add(m.group("named") or m.group("braced"))
    return frozenset(names)


def render_vmoptions(template: str, values: Mapping[str, str]) -> str:
    """The thinkorswim.vmoptions text for a rig cell: the template with every placeholder filled (choice 1).
    ValueError names a placeholder without a value or a value without a placeholder."""
    names = placeholders(template)
    missing, unused = sorted(names - set(values)), sorted(set(values) - names)
    if missing or unused:
        raise ValueError(f"vmoptions template placeholders without a value {missing}, values without a placeholder "
                         f"{unused} (7.3 step 4)")
    return string.Template(template).substitute({k: str(v) for k, v in values.items()})


def verify_vmoptions(text: str, *, pinned_sha256: str, rig_kind: str = "vm",
                     jdk_major: int | None = None) -> tuple[str, ...]:
    """What is wrong with a written thinkorswim.vmoptions (5.3, 12.7 B3 and H10): its sha256 against the rendered
    template's, then `rigfiles.validate_vmoptions`; () when it is the pinned file."""
    got = vmoptions_sha256(text)
    out = [] if got == pinned_sha256 else [f"vmoptions hash {got} is not the pinned {pinned_sha256}"]
    return tuple(out) + validate_vmoptions(text, rig_kind, jdk_major)


def vmoptions_failure(problems: Sequence[str], *, at_creation: bool,
                      installer_log_sha256: str | None = None) -> ControlResult | None:
    """The outcome of a vmoptions check: None without problems; while creating a snapshot `install_failed` with the
    log (7.2, 7.3); when re-applied before a run `env_error (gate: preflight.B3)` on every scoped checkpoint."""
    if not problems:
        return None
    detail = "; ".join(problems)
    if at_creation:
        return install_failed("vmoptions write", detail, installer_log_sha256)
    return ControlResult("vmoptions", False, "preflight.B3", run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", "gate", "preflight.B3", detail), alert=alerts.env_error(),
                         detail=detail, evidence={"problems": list(problems)})


# -- install (7.3) ----------------------------------------------------------------------------------------------
INSTALL_STEPS: dict[str, tuple[str, ...]] = {
    "A": ("fetch_and_verify_artifact", "revert_base_os", "silent_install_auto_update_off", "write_vmoptions",
          "copy_corpus", "first_launch", "read_fingerprint_and_snapshot"),
    "B": ("revert_base_os", "launch_until_updater_done", "read_build_id", "write_vmoptions", "copy_corpus",
          "read_fingerprint_and_snapshot"),
}


def install_failed(step: str, detail: str, installer_log_sha256: str | None) -> ControlResult:
    """`env_error (gate: provisioned, reason: install_failed)` with the installer log attached (7.2, 7.3); no snapshot
    is written."""
    return ControlResult("install", False, "provisioned", run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", "install_failed", "provisioned", f"{step}: {detail}"),
                         alert=alerts.env_error(), detail=f"{step}: {detail}",
                         evidence={"step": step, "installer_log": installer_log_sha256})


def install_outcome(exit_code: int, main_exe_present: bool, installer_log_sha256: str | None) -> ControlResult | None:
    """7.3 step 3: a non-zero installer exit or a missing main executable afterwards is `install_failed`."""
    if exit_code == 0 and main_exe_present:
        return None
    why = f"installer exit {exit_code}" if exit_code != 0 else "the main executable is missing after the install"
    return install_failed("silent_install_auto_update_off", why, installer_log_sha256)


def first_launch_agreement(operator: str | None) -> ControlResult:
    """7.3 step 6: an agreement dialog (POP-03) stops the build as `env_error (gate: authenticated, reason:
    agreement_pending)`; the accounts owner accepts it at the hypervisor console and the operator's name is logged."""
    return ControlResult("first_launch", False, "authenticated", run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", "agreement_pending", "authenticated"),
                         alert=alerts.env_error(notified=("accounts_owner",)),
                         detail="an agreement dialog appeared at the first launch; the harness never accepts one",
                         evidence={"operator": operator})


# -- the rig pool (5.3, 7.1, 11.2) --------------------------------------------------------------------------------
class PoolError(ValueError):
    """A pool change that would break a rule of 5.3 or 11.2 (a login on two rigs, a substitution without a fresh
    snapshot, an Explorer session while the spare serves the pool)."""


@dataclass(frozen=True)
class RigSlot:
    """One rig of the fleet (7.1, 11.2): its role, login, base-os generation and current snapshot."""

    rig_id: str
    role: str
    login: str | None
    gen: str
    snapshot_id: str
    active: bool = True

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"rig role {self.role!r} is not one of {ROLES}")


@dataclass(frozen=True)
class PoolEvent:
    """A run-health line for a pool change: what, which rigs, the operator and when (5.3, 11.2)."""

    action: str                      # substitute | release_spare | assign
    rig_id: str
    operator: str
    at: str
    from_rig: str | None = None
    login: str | None = None
    snapshot_id: str | None = None


@dataclass
class RigPool:
    """The fleet's logins and roles with the spare rule (module docstring, choice 2)."""

    rigs: dict[str, RigSlot]
    spare: str = SPARE_RIG
    events: list[PoolEvent] = field(default_factory=list)
    substituted: str | None = None              # the failed rig the spare stands in for

    @classmethod
    def of(cls, slots: Iterable[RigSlot], *, spare: str = SPARE_RIG) -> RigPool:
        pool = cls({s.rig_id: s for s in slots}, spare)
        if spare not in pool.rigs:
            raise PoolError(f"the spare {spare} is not a rig of the fleet")
        pool._check()
        return pool

    def _check(self) -> None:
        seen: dict[str, str] = {}
        for s in self.rigs.values():
            if s.active and s.login is not None:
                if s.login in seen:
                    raise PoolError(f"login {s.login} is live on {seen[s.login]} and {s.rig_id}; a login is never live "
                                    "on two rigs (5.3, 11.2)")
                seen[s.login] = s.rig_id

    def _set(self, slot: RigSlot) -> None:
        before = dict(self.rigs)
        self.rigs[slot.rig_id] = slot
        try:
            self._check()
        except PoolError:
            self.rigs = before
            raise

    def assign(self, rig_id: str, login: str, *, operator: str, at: str) -> PoolEvent:
        """Put a login on a rig; PoolError when it is live on another rig."""
        self._set(replace(self.rigs[rig_id], login=login))
        ev = PoolEvent("assign", rig_id, operator, at, login=login)
        self.events.append(ev)
        return ev

    @property
    def explorer_allowed(self) -> bool:
        """False while the spare serves the pool: no Explorer session runs then (5.3, 11.2)."""
        return self.substituted is None

    def require_explorer(self) -> None:
        """Raise PoolError when an Explorer session would start while the spare serves the pool."""
        if not self.explorer_allowed:
            raise PoolError(f"{self.spare} serves the pool in place of {self.substituted}: no Explorer session (5.3)")

    def substitute(self, failed: str, *, operator: str, snapshot_id: str, at: str) -> PoolEvent:
        """The spare replaces a failed pool rig (5.3, choice 2): re-snapshotted with its login (`snapshot_id` must be
        new), the failed rig deactivated, the Explorer disabled, the operator recorded."""
        if not operator:
            raise PoolError("a substitution records the operator who did it (5.3)")
        if self.substituted is not None:
            raise PoolError(f"{self.spare} already stands in for {self.substituted}")
        old, spare = self.rigs[failed], self.rigs[self.spare]
        if failed == self.spare or old.role != "pool" or not old.active:
            raise PoolError(f"{failed} is not an active pool rig; the spare replaces only a failed pool rig (5.3)")
        if not snapshot_id or snapshot_id == spare.snapshot_id:
            raise PoolError("the spare is re-snapshotted with the failed rig's login first: a new snapshot id (5.3)")
        before = dict(self.rigs)
        self.rigs[failed] = replace(old, active=False)
        try:
            self._set(replace(spare, role=old.role, login=old.login, snapshot_id=snapshot_id, gen=old.gen))
        except PoolError:
            self.rigs = before
            raise
        self.substituted = failed
        ev = PoolEvent("substitute", self.spare, operator, at, from_rig=failed, login=old.login,
                       snapshot_id=snapshot_id)
        self.events.append(ev)
        return ev

    def release_spare(self, *, explorer_login: str, snapshot_id: str, operator: str, at: str,
                      gen: str | None = None) -> PoolEvent:
        """Rig 6 returns to Explorer duty with the Explorer login on a fresh explorer snapshot; the failed rig stays
        inactive until it is assigned again (choice 2)."""
        if self.substituted is None:
            raise PoolError(f"{self.spare} is not standing in for a pool rig")
        spare = self.rigs[self.spare]
        self._set(replace(spare, role="explorer", login=explorer_login, snapshot_id=snapshot_id,
                          gen=gen or spare.gen))
        ev = PoolEvent("release_spare", self.spare, operator, at, from_rig=self.substituted, login=explorer_login,
                       snapshot_id=snapshot_id)
        self.substituted = None
        self.events.append(ev)
        return ev

    def requeue_target(self, failed: str, *, gen: str | None = None, busy: Iterable[str] = ()) -> str | None:
        """A pool rig for a re-queued shard (7.2, 10.7, choice 3): active, of role pool, the failed rig's base-os
        generation, not busy; None when there is none."""
        want = gen or self.rigs[failed].gen
        taken = set(busy) | {failed}
        free = sorted(s.rig_id for s in self.rigs.values()
                      if s.active and s.role == "pool" and s.gen == want and s.rig_id not in taken)
        return free[0] if free else None


# -- the rig heartbeat (5.3, 7.11, 10.7) --------------------------------------------------------------------------
@dataclass(frozen=True)
class RigHeartbeat:
    """One Rig Manager heartbeat to the control host (5.3: every 30 s)."""

    rig_id: str
    mono_ts: float
    state: str
    run_id: str | None = None


class HeartbeatSender(Protocol):
    """Carries a rig heartbeat to the control host (the transport is the rig channel's)."""

    def send(self, hb: RigHeartbeat) -> None:
        """Deliver one heartbeat."""


@dataclass
class HeartbeatPacer:
    """Sends a rig's heartbeat every 30 s (choice 4)."""

    rig_id: str
    clock: Clock
    interval_s: float = HEARTBEAT_INTERVAL_S
    last_sent: float | None = None

    def due(self) -> bool:
        return self.last_sent is None or self.clock.monotonic() - self.last_sent >= self.interval_s

    def tick(self, sender: HeartbeatSender, state: str, run_id: str | None = None) -> bool:
        """Send when due; True when a heartbeat went out."""
        if not self.due():
            return False
        now = self.clock.monotonic()
        sender.send(RigHeartbeat(self.rig_id, now, state, run_id))
        self.last_sent = now
        return True


@dataclass
class HeartbeatMonitor:
    """The control host's view of rig liveness: three minutes without a heartbeat is a lost rig (7.11, 10.7)."""

    clock: Clock
    last: dict[str, float] = field(default_factory=dict)

    def watch(self, rig_id: str) -> None:
        """Start counting silence for a rig (choice 4)."""
        self.last.setdefault(rig_id, self.clock.monotonic())

    def receive(self, hb: RigHeartbeat) -> None:
        self.last[hb.rig_id] = self.clock.monotonic()

    def silence(self, rig_id: str) -> float:
        return self.clock.monotonic() - self.last[rig_id]

    def lost(self) -> tuple[str, ...]:
        """The watched rigs silent for 3 minutes or more (`degraded.rig_silent`), sorted; `degraded.rig_lost` decides
        each one's shard."""
        return tuple(sorted(r for r in self.last if rig_silent(self.silence(r))))
