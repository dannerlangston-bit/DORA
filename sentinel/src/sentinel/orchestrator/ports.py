"""The ports: what the platform-independent code needs from Windows, thinkorswim and the rig host, one narrow
Protocol each, with the frozen observations they return.

The Windows track implements these over Win32, DWM, the Java Access Bridge, `SendInput`, the hook host's shared
segment, psutil, the vault, the hypervisor and the traffic shaper; `SimulatedRig` grows fakes of them. Nothing
here decides anything: every rule over these observations lives in the modules that read them (capture, gateway,
executor, fingerprint, preflight, watchdog, chaos). Nothing here imports a Windows-only module.

Observation types that `rig.py` already defines and the Orchestrator already consumes (`WindowInfo`,
`AccountState`, `LoginObservation`) are extended here by subclassing, so a port's richer observation is still the
type the existing gate checks accept.

Choices where the spec is ambiguous or silent:
- SD-57: the attestation is a stage, not a boolean. `ATTESTATION_STAGES` orders `none` (nothing attested),
  `a3_a8` (A3 to A8 passed: the A9 drill click may run) and `a3_a9` (A3 to A9 passed: a later Confirm and Send may
  run) (6.3, 6.5, 5.4.3 stage 4).
- `VaultPort` hands a secret only through `secret(lease)` to the secret step that types it; nothing else holds it,
  and no observation here carries one (6.9).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from . import rig as _rig
from .gateway import Action, Flag, InputEvent

if TYPE_CHECKING:
    from ..validation import ProbeListing

Rect = tuple[int, int, int, int]                 # x, y, w, h in virtual-screen pixels
ATTESTATION_STAGES = ("none", "a3_a8", "a3_a9")   # SD-57, in order


# -- observations -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowInfo(_rig.WindowInfo):
    """One top-level window (3.4, 5.4.6, 5.7): the enumeration fields plus DWM bounds, z-order, owner and the
    process image name, for target selection, occlusion and the Window Watcher."""

    rect: Rect | None = None                     # DWMWA_EXTENDED_FRAME_BOUNDS
    z_order: int | None = None                   # 0 = topmost
    owner_hwnd: int | None = None
    process_name: str | None = None              # image name, e.g. thinkorswim.exe
    enabled: bool = True                         # IsWindowEnabled
    topmost: bool = False
    monitor: int | None = None


@dataclass(frozen=True)
class Frame:
    """One grab of the virtual screen (5.7): `image` is an H x W x 3 uint8 BGR array, `origin` the virtual-screen
    position of pixel (0, 0), `mono_s` the monotonic time of the grab."""

    image: Any
    origin: tuple[int, int]
    mono_s: float


@dataclass(frozen=True)
class PointOwner:
    """`WindowFromPoint` at one sample point of the 8 x 8 occlusion grid (3.4)."""

    x: int
    y: int
    hwnd: int | None
    pid: int | None
    title: str = ""


@dataclass(frozen=True)
class AccessNode:
    """One Java Access Bridge context (DR-01 Branch A, 5.9): role, name, rect, states and children."""

    role: str
    name: str
    rect: Rect | None
    states: frozenset[str] = frozenset()
    children: tuple[AccessNode, ...] = ()
    opaque: bool = False                         # a custom-painted container exposing no children (2.6)


@dataclass(frozen=True)
class ProcessInfo:
    """One process of the launched tree (5.14, 10.2, 10.11): identity and the soak sample metrics."""

    pid: int
    ppid: int | None
    name: str
    exe: str | None = None
    alive: bool = True
    working_set: int | None = None
    private_bytes: int | None = None
    gdi_objects: int | None = None
    user_objects: int | None = None
    handles: int | None = None
    threads: int | None = None
    cpu_s: float | None = None


@dataclass(frozen=True)
class VaultLease:
    """A vault lease on a login's secret (6.9, 7.7): the alias and lease id, never the value."""

    alias: str
    lease_id: str
    expires_at: str


@dataclass(frozen=True)
class KillRequest:
    """A kill-switch request from a human channel (6.6): who asked, through which channel, for which rig."""

    rig_id: str
    channel: str                                 # cli | report_ui | slack | file
    requested_by: str
    reason: str
    at: str


@dataclass(frozen=True)
class OsReads:
    """The OS-class fingerprint reads at gate `provisioned` (4.12, 7.4); None marks a field that could not be
    read (`fingerprint_unreadable:<field>`)."""

    windows_build: Mapping[str, str] | None
    display: Mapping[str, Any] | None
    cleartype: Mapping[str, Any] | None
    gpu: Mapping[str, str] | None
    os_theme: Mapping[str, bool] | None
    keyboard: Mapping[str, Any] | None
    time_zone_os: str | None
    input_desktop: str | None
    rig: Mapping[str, Any] | None = None         # record class: hostname, hypervisor, snapshot ids, VM image


@dataclass(frozen=True)
class AppReads:
    """The app-class fingerprint reads at gate `launched` (4.12, 7.3, 7.4); None marks an unreadable field."""

    build_id_title: str | None
    build_id_about: str | None
    jre_release: Mapping[str, str] | None
    vmoptions_hash: str | None
    java2d_pipeline: Mapping[str, str] | None
    app_theme: Any
    workspace_hash: Mapping[str, str] | None
    settings_profile_hash: str | None
    time_zone_app: Mapping[str, str] | None      # app_display and chart
    render_probe_hash: str | None
    jar_listing_hash: str | None = None          # record class


@dataclass(frozen=True)
class PreflightRead:
    """What one 12.7 item read on the rig: the raw values, or unreadable with the reason."""

    item: str
    values: Mapping[str, Any] = field(default_factory=dict)
    readable: bool = True
    detail: str | None = None


@dataclass(frozen=True)
class ShapingRule:
    """One chaos rule of the traffic shaper (7.11, 10.2): what is done to which host, for how long."""

    kind: str                                    # latency | loss | blackhole
    host: str
    value: float = 0.0                           # ms of latency, or the loss fraction
    duration_s: float = 0.0


@dataclass(frozen=True)
class ConnectivityObservation:
    """The connectivity read (7.11, 7.8): the status-bar text and what it says."""

    status_text: str | None
    connected: bool | None                       # None when unreadable
    backend_degraded: bool = False
    maintenance_notice: bool = False             # POP-09


@dataclass(frozen=True)
class DelayBadge:
    """The quote delay badge read at `fixture_verified` (4.9, 7.9)."""

    text: str | None
    delayed: bool | None                         # None when unreadable
    delay_minutes: int | None = None
    confidence: float = 100.0


@dataclass(frozen=True)
class AccountState(_rig.AccountState):
    """`verify_account_fixture` for one account (7.6), extended with buying power, the run's own tagged Working
    rows, alert count, watchlists and the delay badge (4.9, 6.6, 7.5, 7.9)."""

    buying_power: float | None = None
    tagged_working: tuple[_rig.OrderRow, ...] = ()
    alert_count: int | None = None
    watchlists: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    delay_badge: DelayBadge | None = None


@dataclass(frozen=True)
class LoginObservation(_rig.LoginObservation):
    """The LOGIN flow's exits (7.7), extended with the agreement prompt (`agreement_pending`, 7.3) and the
    OnDemand toolbar seen after login (7.9)."""

    agreement_pending: bool = False
    ondemand_toolbar: bool = False


@dataclass(frozen=True)
class LocateResult:
    """The locator ladder's answer for one target (5.5): the point and rect it resolved, the rung that resolved
    it (`none` when every rung failed) and every rung tried, as 4.13 step_outcome.rung_results."""

    rung: str                                    # jab | image | ocr | agent | none
    point: tuple[int, int] | None = None
    rect: Rect | None = None
    rung_results: tuple[Mapping[str, Any], ...] = ()
    finding_kind: str | None = None              # locator_failure when the scripted rungs all failed


@dataclass(frozen=True)
class SessionSafety:
    """The session's safety state the slow half reads (5.4.3 stage 4, 6.3, 6.4, SD-57): how far attestation got
    and when the paper-mode assertion last passed (monotonic seconds; None before the first)."""

    attestation_stage: str = "none"
    last_assertion_mono: float | None = None
    account_id_sha256: str | None = None
    session_kicked: bool = False

    def __post_init__(self) -> None:
        if self.attestation_stage not in ATTESTATION_STAGES:
            raise ValueError(f"attestation stage {self.attestation_stage!r}; one of {ATTESTATION_STAGES} (SD-57)")

    def attested_through(self, stage: str) -> bool:
        """True when attestation reached `stage` or beyond (SD-57: the A9 click needs a3_a8, a later send a3_a9)."""
        return ATTESTATION_STAGES.index(self.attestation_stage) >= ATTESTATION_STAGES.index(stage)


# -- ports --------------------------------------------------------------------------------------------------------

@runtime_checkable
class DesktopPort(Protocol):
    """Windows, frames and point ownership (3.4, 5.4.6, 5.7, 10.2)."""

    def enumerate_windows(self) -> tuple[WindowInfo, ...]:
        """Top-level windows of the launched process tree, visible, SunAwt classes first (5.4.6)."""

    def grab(self) -> Frame:
        """One grab of the virtual screen (5.7)."""

    def window_from_point(self, points: Sequence[tuple[int, int]]) -> tuple[PointOwner, ...]:
        """`WindowFromPoint` for each sample point (3.4 occlusion grid)."""

    def virtual_screen(self) -> Rect:
        """The virtual screen rectangle (3.4: a target outside it is window_mismatch)."""

    def process_dpi_awareness(self, pid: int) -> str:
        """The process's DPI awareness (7.1, H3: PER_MONITOR_AWARE_V2 expected)."""

    def input_desktop(self) -> str:
        """The OpenInputDesktop name (4.12: must be Default)."""

    def close_window(self, hwnd: int) -> bool:
        """`WM_CLOSE` to one window (the popup sentinel's close of an unknown or error dialog, 4.10, 7.8); False when
        the window stayed."""

    def foreground_window(self) -> WindowInfo | None:
        """`GetForegroundWindow` with its owner and image name (7.8 hardening, before every input batch)."""


@runtime_checkable
class InputPort(Protocol):
    """`SendInput` for the gateway's tokened events (5.4.3 stage 6); never called outside the emitter."""

    def send(self, events: Sequence[InputEvent], chars: Sequence[str | None] | None = None) -> int:
        """Inject the events; returns how many the OS accepted. `chars` runs parallel to `events`: the typed character
        of each `VK_PACKET` key event (the planner's `Emit.char`, 5.4.3), None for every other event; an
        `InputEvent` carries no character, so the port takes it here (windows-adapters choice 2)."""

    def cursor(self) -> tuple[int, int]:
        """The cursor position."""


@runtime_checkable
class SegmentPort(Protocol):
    """The hook host's shared-memory segment (5.4.2): header, token slots, deny rects, chords, drop ring."""

    def read(self) -> bytes:
        """A consistent copy of the whole segment."""

    def write(self, offset: int, data: bytes) -> None:
        """Write bytes at an offset (the seqlock is the codec's, not the port's)."""


@runtime_checkable
class AccessPort(Protocol):
    """The Java Access Bridge (DR-01 Branch A, 5.9, 11.8)."""

    def available(self) -> bool:
        """True when the bridge answers for the main frame."""

    def tree(self, hwnd: int) -> AccessNode | None:
        """The accessible tree of a window; None when the bridge is down."""

    def focus_owner(self, hwnd: int) -> AccessNode | None:
        """The focused accessible context (the keyboard-only ticket variant, 2.6)."""


@runtime_checkable
class ProcessPort(Protocol):
    """Processes, crash artifacts and the kill (5.14, 6.6, 10.2, 10.11)."""

    def process_tree(self, root_pid: int) -> tuple[ProcessInfo, ...]:
        """The launched tree with its sample metrics."""

    def kill_tree(self, root_pid: int) -> bool:
        """Kill children first; True when nothing from the install folder survives (6.6 step 3)."""

    def crash_files(self, since: str) -> tuple[str, ...]:
        """New hs_err_pid files, heap and WER dumps since `since` (10.2)."""


@runtime_checkable
class ClipboardPort(Protocol):
    """The clipboard, for `copy_grid` reads (2.8.1, 5.10)."""

    def read_text(self) -> str | None:
        """The clipboard text; None when empty or not text."""

    def clear(self) -> None:
        """Clear it after the read."""


@runtime_checkable
class VaultPort(Protocol):
    """The credential vault (6.9, 7.7)."""

    def lease(self, alias: str) -> VaultLease:
        """Lease the login's secret; raises when the vault is unreachable (vault_unreachable)."""

    def secret(self, lease: VaultLease) -> str:
        """The secret, for the secret step that types it and nothing else."""

    def release(self, lease: VaultLease) -> None:
        """Release the lease at teardown."""


@runtime_checkable
class KillChannel(Protocol):
    """The transport of the kill switch's human channels (6.6): `sentinel stop <rig>`, the report UI, Slack."""

    def poll(self) -> KillRequest | None:
        """The pending request, if any."""

    def post(self, request: KillRequest) -> None:
        """Raise a request (the CLI and the drill's item 5 use it)."""

    def acknowledge(self, request: KillRequest) -> None:
        """Mark a request handled."""


@runtime_checkable
class FingerprintReader(Protocol):
    """The live reads behind the 4.12 fingerprint (7.4)."""

    def read_os(self) -> OsReads:
        """OS fields, at gate `provisioned`."""

    def read_app(self) -> AppReads:
        """App fields, at gate `launched` and after a relaunch."""


@runtime_checkable
class RegistryProbes(Protocol):
    """Registry validation's probes P1 to P8 (2.2.3, SD-15): at entry to `running`, in the shard's first tier A
    workspace, each probe OCRs its declared list on one build."""

    def read_probe(self, probe: str, side: str) -> ProbeListing:
        """The labels probe `probe` lists on build `side` (`candidate` or `base`), in list order, and whether its
        anchor region was located; `screenshot` is the probe screenshot's sha256 once stored."""


@runtime_checkable
class PreflightReads(Protocol):
    """The raw reads behind the 12.7 items; the evaluation is the preflight module's."""

    def read(self, item: str) -> PreflightRead:
        """Read one item (R1, H7, ...)."""


@runtime_checkable
class Hypervisor(Protocol):
    """Snapshots of the rig VM (7.2, 6.6 step 6, 11.2)."""

    def revert(self, snapshot_id: str) -> bool:
        """Revert to a snapshot; False when it failed (snapshot_revert_failed)."""

    def snapshot(self, name: str) -> str:
        """Take a snapshot; returns its id."""

    def snapshots(self) -> tuple[str, ...]:
        """The snapshot ids of the VM."""


@runtime_checkable
class TrafficShaper(Protocol):
    """Network chaos for the RC chaos stage (7.11, 10.2)."""

    def apply(self, rule: ShapingRule) -> None:
        """Start a rule."""

    def clear(self) -> None:
        """Remove every rule."""

    def active(self) -> tuple[ShapingRule, ...]:
        """The rules in force (chaos_rule_stuck when one outlives its duration)."""


@runtime_checkable
class OrderBookRead(Protocol):
    """Monitor reads for the proof and the fixture check (6.6, 7.6)."""

    def read_order_book(self) -> _rig.OrderBook:
        """Working, Filled and Canceled rows, Position Statement rows and Net Liq."""

    def copy_all_text(self) -> str | None:
        """The Copy All text of the Working grid, for the cross-check (6.6)."""


@runtime_checkable
class ConnectivityRead(Protocol):
    """The status-bar connectivity read (7.11)."""

    def read_connectivity(self) -> ConnectivityObservation:
        """The current status."""


@runtime_checkable
class DelayBadgeRead(Protocol):
    """The quote delay badge (4.9, 7.9)."""

    def read_delay_badge(self) -> DelayBadge:
        """The badge as read now."""


@runtime_checkable
class Locator(Protocol):
    """The locator ladder of 5.5: JAB, image, OCR, then the agent rescue when allowed."""

    def locate(self, target: Mapping[str, Any], frame: Frame) -> LocateResult:
        """Resolve a step's target on a frame."""


@runtime_checkable
class PrimitivePlanner(Protocol):
    """Turns a resolved step into the gateway actions that perform it (5.4.3 stages 1 to 3)."""

    def plan(self, step: Mapping[str, Any], points: Mapping[str, tuple[int, int]], flag: Flag,
             now: float) -> tuple[Action, ...]:
        """The actions for one step, in order."""
