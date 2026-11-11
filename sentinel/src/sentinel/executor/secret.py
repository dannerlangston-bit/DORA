"""Secret steps (5.4.10, 6.9, 7.7): a credential typed through the gateway without ever becoming a record.

`run_secret_step(step, vault, *, flow_class, emit, capture, now)` runs one `secret(target, vault_ref, commit)` step:

1. refuses it outside a `class: login` flow (4.7, L-FLW-06) before anything is leased;
2. leases the secret at the last moment through the `ports.VaultPort` (`lease`, then `secret`); a failure is retried
   once after 30 s, and a second failure means nothing is typed and no login is attempted: `env_error (gate:
   authenticated, reason: vault_unreachable)` on every scoped checkpoint, run `complete_with_env_errors`, the system
   owner paged and the on-call channel posted at P2 (P1 when the previous run failed the same way, 9.10) (6.9, 7.7);
3. plans the key pairs with `secret` tokens (`primitives.Planner.plan_secret`), suspends Capture
   (`CaptureControl.suspend_capture`: `capture_suspended: true` in the heartbeat, no Watchdog screenshot, the ffmpeg
   ring and the Recorder hook paused), hands the action to `emit` (the executor's stages 4 to 7) and resumes Capture
   whatever happened;
4. returns a `SecretResult` whose step event shows `<redacted:<vault_ref>>` and a character count, nothing else.

The value lives in local variables of this call and in the planned action's units only while they are emitted; it
never reaches the clipboard, a log line or a record. The lease is kept on the result: the teardown plaintext scan
re-reads the value under it (6.9 Plaintext scan) and then releases it (`VaultPort.release` "at teardown"). Agent
fallback stays hard-disabled until the post-login checkpoint through `locator.LadderContext.post_login`.

Choices where the spec is ambiguous or silent (reported):

1. The vault alias leased is the step's `vault_ref` (`login.password`), the name the redacted placeholder shows.
2. A failure of either vault call (`lease` or `secret`) counts as the lease failing: any exception from the port is
   the vault being unreachable, since the port's contract names no narrower error.
3. The character count is written in the step event's `keys` column as `<n> chars`, followed by ` + <commit>` when
   the step commits with Enter or Tab (8.3 has no count column).
4. A value the planner cannot type (a space or control character, primitives choice 1) fails the step before Capture
   is suspended; the reason never quotes the value.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..orchestrator import alerts
from ..orchestrator.ports import VaultLease, VaultPort
from ..orchestrator.results import ControlResult, Outcome, RunVerdict
from ..records import REDACTED, StepEvent
from ..taxonomy import load
from .primitives import PlanError, PlannedAction, Planner

LEASE_RETRY_S = 30.0               # 6.9: a lease that fails is retried once after 30 seconds


class CaptureControl(Protocol):
    """What a secret step suspends (5.4.10, 6.9): screenshots and recording, the Recorder hook, and the heartbeat's
    `capture_suspended` flag the Watchdog reads before it takes a screenshot."""

    def suspend_capture(self) -> None:
        """`capture_suspended: true`: no screenshot, no recording, Recorder hook paused."""

    def resume_capture(self) -> None:
        """`capture_suspended: false`: everything suspended runs again."""


Emit = Callable[[PlannedAction], str]
"""The executor's emission of one planned action through the gateway (5.4.3 stages 4 to 7): its step outcome."""


@dataclass(frozen=True)
class SecretResult:
    """What one secret step did: its step outcome, the vault reference, how many characters were typed, the lease
    (for the teardown scan and release), the run consequence of an unreachable vault and the reason it failed."""

    status: str
    vault_ref: str
    chars: int = 0
    commit: str = "none"
    lease: VaultLease | None = None
    consequence: ControlResult | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        load().check_step(self.status)

    @property
    def target_text(self) -> str:
        """The step event's target text: `<redacted:login.password>` (6.9, 8.3)."""
        return f"<redacted:{self.vault_ref}>"

    @property
    def keys_text(self) -> str:
        """The character count and the commit key (choice 3)."""
        return f"{self.chars} chars" + (f" + {self.commit}" if self.commit != "none" else "")

    def step_event(self, *, run_id: str, shard_id: str, side: str, seq: int, wall_at: str, mono_ms: int,
                   flow_id: str, step_id: str, duration_ms: int | None = None) -> StepEvent:
        """The step's `secret` step event (8.3): the redacted placeholder and the count, never the value."""
        return StepEvent(run_id=run_id, shard_id=shard_id, side=side, seq=seq, source="orchestrator",
                         wall_at=wall_at, mono_ms=mono_ms, mode="scripted", action="secret", flow_id=flow_id,
                         step_id=step_id, step_outcome=self.status, target_text=self.target_text,
                         keys=self.keys_text, reason=self.detail, duration_ms=duration_ms)


def vault_unreachable(*, repeat: bool = False, detail: str | None = None) -> ControlResult:
    """A lease that failed twice (6.9, 7.7): nothing typed, no login attempted, `env_error (gate: authenticated,
    reason: vault_unreachable)` on every scoped checkpoint, run `complete_with_env_errors`, the system owner paged
    and the on-call channel posted (P2; P1 when the previous run failed the same way, 9.10; SD-62)."""
    return ControlResult("secret_step", False, "authenticated", run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", "vault_unreachable", "authenticated", detail),
                         alert=alerts.env_error(repeat=repeat).plus(paged=("system_owner",)), step="failed",
                         actions=("teardown",), detail=detail)


def run_secret_step(step: Mapping[str, Any], vault: VaultPort, *, flow_class: str | None, emit: Emit,
                    capture: CaptureControl, now: float, sleep: Callable[[float], None] = time.sleep,
                    repeat: bool = False) -> SecretResult:
    """Run one secret step (module docstring). `repeat` says the previous run ended on `vault_unreachable` too."""
    if step.get("type") != "secret":
        raise ValueError("run_secret_step runs secret steps")
    ref = step.get("vault_ref")
    commit = str(step.get("commit", "none"))
    if not isinstance(ref, str) or not REDACTED.match(f"<redacted:{ref}>"):
        raise ValueError("a secret step names its vault_ref: no spaces or angle brackets (8.3 '<redacted:...>')")
    if flow_class != "login":
        return SecretResult("failed", ref, commit=commit,
                            detail="a secret step runs only in a class: login flow (4.7, L-FLW-06); nothing leased")
    lease, value = _lease(vault, ref, sleep)
    if lease is None or value is None:
        detail = "the vault lease failed twice, 30 s apart; nothing typed"
        return SecretResult("failed", ref, commit=commit, consequence=vault_unreachable(repeat=repeat, detail=detail),
                            detail="vault_unreachable")
    try:
        action = Planner(profile="scripted", flow_class="login").plan_secret(step, value, now)
    except PlanError as exc:
        return SecretResult("failed", ref, commit=commit, lease=lease, detail=f"the secret cannot be typed: {exc}")
    finally:
        del value
    capture.suspend_capture()
    try:
        status = emit(action)
    finally:
        capture.resume_capture()
    chars = action.chars()
    del action
    return SecretResult(status, ref, chars, commit, lease)


def _lease(vault: VaultPort, ref: str, sleep: Callable[[float], None]) -> tuple[VaultLease | None, str | None]:
    """Lease and read the secret, once more after 30 s when the first try fails (6.9; choice 2)."""
    for attempt in range(2):
        if attempt:
            sleep(LEASE_RETRY_S)
        try:
            lease = vault.lease(ref)
        except Exception:                      # noqa: BLE001  the port names no narrower error (choice 2)
            continue
        try:
            return lease, vault.secret(lease)
        except Exception:                      # noqa: BLE001
            try:
                vault.release(lease)           # a lease whose secret could not be read is not kept
            except Exception:                  # noqa: BLE001  the vault is failing already; the lease expires
                pass
    return None, None
