"""The Watchdog process: its 2 s sample, its reaction, its reverse heartbeat and its journal (10.2, 5.14, 5.12, 6.6).

The Watchdog is a separate process, started at rig logon before the Orchestrator, that emits no input and never opens
the Run Store (10.2 Placement, 8.2). `watchdog.py` holds the decisions it shares with the Orchestrator; this module
is the process around them, platform-independent: every OS read comes through `ports.DesktopPort`, `ports.ProcessPort`
and the narrow `WatchdogHost` below, which the Windows track implements (`win32.process.Win32Processes`,
`win32.desktop.Win32Desktop`, plus jcmd, the Event Log, the client log, the named event and process start).

- `Observation`: one sample's reads. `WatchdogLoop.observe()` builds it; it takes no screenshot and computes no hash
  while the heartbeat says `capture_suspended` (10.2 Heartbeats).
- `Watchdog.sample(now, obs) -> Reaction | None`: the decision, one reaction per sample, in this precedence: the local
  dead-man (deadline plus grace passed before `reported`: kill the Orchestrator, the tree and the hook host, run
  `orchestrator --resume <run_id> --finalize`); the kill switch's second half (the file flag, the named pipe, or a dead
  Orchestrator while the ticket flag is set: `deny_all`, kill the tree, hand the recovery to the Orchestrator, 6.6);
  an Orchestrator death or hang (heartbeat over 120 s in `running`, over 5 minutes in `provisioned` and `launched`;
  `watchdog.orchestrator_died` spends one of 2 restarts and the reaction starts `orchestrator --resume <run_id>`, or,
  spent, ships the shard as it is, 8.2); a dead hook host (reported to the Orchestrator, which fails closed, 5.4.2); an
  app failure (new crash files, Event Log 1000 or 1002 for a tree process, an `OutOfMemoryError` in the client log,
  the tree's working set over 1.5 x `-Xmx` plus 1 GB, a process's GDI objects at 9,000, screenshot staleness by
  `executor.heartbeat.hang_detected`, `IsHungAppWindow`, a JAB query over 5 s, or an empty tree; the cause is
  `watchdog.crash_signal`'s, `oom` first); `main_frame_missing` (no frame matches the title regex for 30 s while the
  heartbeat says `running`: the tree is then tracked by the install-folder rule alone); the `launched` gate failure
  (no frame for 5 minutes after launch while a process from the install folder is alive).
- `Reaction.steps`: the 10.2 Reaction order, one sequence for hang, crash and Orchestrator death: 1 a full
  virtual-screen screenshot to the restricted folder (skipped while `capture_suspended`), 2 `jcmd Thread.print` and
  `GC.heap_info` under a 15 s timeout, 3 the collection (client log slice since the last successful checkpoint,
  hs_err_pid files, dumps, Event Log entries, process metrics, both heartbeat files, the last 20 gateway events), 4
  `reset_input_state` through a fresh gateway instance, then the kill, children first, and the hook host last only
  when the Orchestrator is dead, 5 one `watchdog_events.jsonl` line, 6 the named-event signal, or the resume when the
  Orchestrator is dead. `run_reaction` executes them and returns the evidence manifest with its "unavailable" notes
  ("thread dump unavailable: attach disabled", "client log: not located").
- `StatusWriter` writes `watchdog.json` (pid, monotonic time, last trigger, free bytes) every 2 s; `status_check` is
  the Orchestrator's read for `watchdog.watchdog_check` (age over 30 s or a dead PID ends the run `watchdog_dead`), and
  `crash_signal_of` turns the last trigger back into the `rig.CrashSignal` the Orchestrator's `watchdog.app_failure`
  takes.
- `Journal` appends `watchdog_events.jsonl` in the line format of `store.journal` (its `WATCHDOG_ACTIONS`); a failed
  append writes Application Event Log event 9001 (source Sentinel) and raises the named event (10.2).
- `WatchdogLoop.tick()` is one sample: observe, decide, react, write the status; `run(stop)` repeats it every 2 s.

Choices where the spec is ambiguous or silent (reported):

1. Heartbeat age is the Watchdog's monotonic time minus the heartbeat's `mono_ts`: both processes read the system-wide
   monotonic clock (`GetTickCount64` behind `time.monotonic` on Windows), so a wall-clock change can neither fake nor
   hide staleness (10.2). A missing heartbeat file triggers nothing: the Watchdog starts before the Orchestrator, and
   the dead-man covers an Orchestrator that never writes one.
2. After an Orchestrator reaction the PID of a heartbeat older than the reaction is not checked again, and staleness
   counts from the reaction until the resumed Orchestrator writes its own heartbeat; a resumed Orchestrator that never
   writes one is a hang again and spends the next restart.
3. An empty tree is the `process_exit` app failure only while the heartbeat names an `app_pid` whose tree this
   Watchdog saw alive: the Orchestrator clears `app_pid` in its heartbeat before it kills the tree itself (a relaunch,
   the kill switch, teardown), so an empty tree it asked for is never an app failure. The launcher log and the build
   manifest are read (`WatchdogHost.version_changed`) only then, and their answer rides on the `CrashSignal`.
4. Working set: the sum over the tree against 1.5 x `-Xmx` plus 1 GiB, cause `oom` (source `working_set`); GDI: any
   one process at 9,000 or more, cause `hang` (source `gdi_objects`: exhaustion paints nothing, 10.2 sampling 4).
   Event Log 1000 is cause `jvm_crash`, 1002 `hang`; `IsHungAppWindow` and the JAB stall are cause `hang` (sources
   `is_hung_app_window`, `jab_timeout`). An entry without a PID is counted (the host filters by the install folder).
5. `main_frame_missing` and the `launched` gate failure are recorded in `watchdog.json`'s last trigger, which the
   Orchestrator reads at every gate and step, and the `launched` failure is also signalled. `main_frame_missing` is
   not journaled: `step_event_actions` (and so `store.journal.WATCHDOG_ACTIONS`) has no action for it, and the journal
   carries only the closed vocabulary (reported: the action is needed for 10.2's "journals main_frame_missing").
   The 5 minutes run from the first sample whose heartbeat says `launched`.
6. Journal action per reaction: an app hang `watchdog_hang`; any other app failure, and an Orchestrator death after
   its restart budget is spent, `watchdog_crash`; an Orchestrator death or hang with a restart `resume`; the local
   dead-man `resume` with a detail starting `finalize`; the kill switch `kill_switch`. A hook host death is signalled,
   not journaled (the Orchestrator records its own outcome, 5.4.2). Event ids are `wd-NNNN`, continuing the journal.
7. The hung Orchestrator is killed first in reaction step 4, before `reset_input_state`, so nothing it still holds can
   inject after the reset. The kill switch keeps the hook host up (deny-all covers the recovery); its sequence is
   `deny_all`, the kill, the journal line and the signal, or, when the Orchestrator is dead, the resume that runs the
   6.6 recovery (`resume.ResumePlan.kill_switch_pending`). That resume spends no Orchestrator restart: it resumes no
   work, and the recovery login and verified Cancel All are never refused for a crash budget (6.6).
8. `self_abort` (the Orchestrator's own heartbeat failure) is sticky: from then on neither a stale heartbeat nor the
   Orchestrator's exit starts a resume; the dead-man and the kill switch stay armed.
9. The local dead-man fires once, at the first sample at or after `watchdog.dead_man_deadline`, unless the heartbeat
   says `reported`; `--finalize` gets the gate in force and whether this Watchdog ever saw `running`.
10. The collection's "since the last successful checkpoint" is the wall time of the heartbeat that first carried the
    current `last_capture_sha256` (the settle rule accepted that capture), else the run's first heartbeat.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from ..executor.heartbeat import hang_detected
from ..store.journal import WATCHDOG_ACTIONS, WATCHDOG_JOURNAL
from . import killflag
from .budgets import ShardBudgets
from .clock import Clock
from .killswitch import TripWires, check_tripwires, kill_switch
from .ports import DesktopPort, KillChannel, KillRequest, ProcessInfo, ProcessPort, WindowInfo
from .results import ControlResult
from .rig import CrashSignal
from .watchdog import (Heartbeat, crash_signal, dead_man_deadline, dead_man_finalize, orchestrator_died,
                       orchestrator_hung)

SAMPLE_S = 2.0                                  # 10.2: sampling and watchdog.json, every 2 s
MAIN_FRAME_MISSING_S = 30.0                     # 10.2 sampling 3
LAUNCHED_FRAME_S = 5 * 60.0                     # 10.2 sampling 3: the `launched` gate failure
GDI_LIMIT = 9_000                               # 10.2 sampling 4 (the per-process quota is 10,000)
GIB = 1024 ** 3
THREAD_DUMP_TIMEOUT_S = 15.0                    # 10.2 reaction step 2
GATEWAY_EVENTS = 20                             # 10.2 reaction step 3
EVENT_LOG_IDS = (1000, 1002)                    # application error, application hang (10.2 sampling 6)
JOURNAL_FAILED_EVENT = 9001                     # 10.2: source Sentinel
EVENT_SOURCE = "Sentinel"
FRAME_CLASS = "SunAwtFrame"
DEFAULT_TITLE_REGEX = r"Main@thinkorswim \[build \d+\]"    # 10.2: a config value, confirmed in Phase 0 (7.3)
STATUS_FILE = "watchdog.json"
HEARTBEAT_FILE = "heartbeat.json"
# the gates during which the app is up: its failure detectors apply (10.1, 10.2; F62 the first launch included)
APP_STATES = ("launched", "authenticated", "paper_mode_asserted", "settings_attested", "workspace_loaded",
              "fixture_verified", "canary_flow_passed", "running", "teardown")
RUNNING_STATES = ("running", "teardown", "reported")
# the named-event codes the Watchdog raises toward the Orchestrator (10.2 reaction step 6, sampling 2 and 3)
SIGNALS = ("app_failure", "hook_host_dead", "launched_failed", "kill_switch", "journal_failed")
# the steps of a reaction, in the 10.2 order (module docstring)
STEPS = ("screenshot", "thread_dump", "collect", "kill_orchestrator", "deny_all", "reset_input_state", "kill_tree",
         "kill_hook_host", "journal", "signal", "start_resume", "start_finalize", "ship_as_is")
TRIGGERS = ("dead_man", "kill_switch", "orchestrator_died", "orchestrator_hung", "hook_host_dead", "app_failure",
            "main_frame_missing", "launched_failed")
THREAD_DUMP_STATUSES = ("ok", "timeout", "attach_disabled", "no_jdk", "no_process")
_UNAVAILABLE = {"timeout": f"thread dump unavailable: timed out after {THREAD_DUMP_TIMEOUT_S:.0f} s",
                "attach_disabled": "thread dump unavailable: attach disabled",
                "no_jdk": "thread dump unavailable: no JDK of the bundled runtime's major version",
                "no_process": "thread dump unavailable: no app process"}


def _iso(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# -- observations -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class EventLogEntry:
    """An Application Event Log entry (10.2 sampling 6): 1000 application error, 1002 application hang."""

    event_id: int
    pid: int | None
    at: str
    message: str = ""


@dataclass(frozen=True)
class Observation:
    """One 2 s sample (10.2 Sampling). `mono` is the Watchdog's monotonic seconds, `wall` the aware UTC time; the
    rest is what the sample read, each None or empty when not read."""

    mono: float
    wall: datetime
    heartbeat: Heartbeat | None = None
    orchestrator_alive: bool | None = None       # the heartbeat's orchestrator_pid; None when no PID is known
    hook_host_alive: bool | None = None
    main_frame: WindowInfo | None = None         # the window matching the title regex, class SunAwtFrame
    tree: tuple[ProcessInfo, ...] = ()           # descended from the launcher or the main frame, or from the folder
    install_alive: bool = False                  # a process running from the pinned install folder is alive
    frame_hash: str | None = None                # the main window region's hash; None when not taken
    anti_anchor: bool = False                    # a spinner, Loading or Reconnecting template on screen
    hung_window: bool = False                    # IsHungAppWindow or SendMessageTimeout(WM_NULL), the secondary check
    jab_stalled: bool = False                    # a JAB query that did not return within 5 s (DR-01 branch A)
    crash_files: tuple[str, ...] = ()            # new since the previous sample
    event_log: tuple[EventLogEntry, ...] = ()    # new 1000 and 1002 entries
    log_oom: bool = False                        # an OutOfMemoryError line in the client log since the last sample
    launcher_update: bool = False                # read only for an empty tree (choice 3)
    build_manifest_changed: bool = False
    free_bytes: int | None = None
    self_abort: bool = False                     # the Orchestrator's named-event code (10.2)
    kill_requests: Mapping[str, KillRequest] = field(default_factory=dict)   # killflag.pending
    ticket_flag: bool = False                    # the hook's order-stage flag (6.6: dead Orchestrator with the flag)


@dataclass(frozen=True)
class WatchdogConfig:
    """The facts of the shard the Watchdog watches. `run_deadline` is the 11.2 run deadline (for a Calibration run, the
    scheduled end of the night's pass, SD-98); `xmx_bytes` the pinned `-Xmx`; `launcher_pid` the PID the Rig Manager
    started."""

    run_id: str
    shard_id: str
    run_type: str
    run_deadline: datetime
    install_dir: str
    shard_dir: str
    title_regex: str = DEFAULT_TITLE_REGEX
    xmx_bytes: int | None = None
    nightly_cutoff: datetime | None = None
    launcher_pid: int | None = None

    @property
    def working_set_ceiling(self) -> int | None:
        """1.5 x `-Xmx` plus 1 GiB (10.2 sampling 4); None when `-Xmx` is not known."""
        return None if self.xmx_bytes is None else int(1.5 * self.xmx_bytes) + GIB

    def resume_argv(self) -> tuple[str, ...]:
        """`orchestrator --resume <run_id>` with the shard directory (10.2 reaction step 6)."""
        return ("--resume", self.run_id, "--shard-dir", self.shard_dir)


# -- the reaction -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Step:
    """One step of a reaction (`STEPS`) with its arguments."""

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in STEPS:
            raise ValueError(f"reaction step {self.name!r}; one of {STEPS}")


@dataclass(frozen=True)
class Reaction:
    """What one trigger makes the Watchdog do, in order (10.2 Reaction). `decision` is the shared decision behind it
    (`orchestrator_died`, `kill_switch`, `dead_man_finalize`), `cause` and `source` an app failure's."""

    trigger: str
    steps: tuple[Step, ...]
    detail: str
    cause: str | None = None
    source: str | None = None
    signal: CrashSignal | None = None
    decision: ControlResult | None = None

    def __post_init__(self) -> None:
        if self.trigger not in TRIGGERS:
            raise ValueError(f"trigger {self.trigger!r}; one of {TRIGGERS}")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.steps)

    def step(self, name: str) -> Step | None:
        return next((s for s in self.steps if s.name == name), None)

    def trigger_record(self, event_id: str | None, mono: float) -> dict[str, Any]:
        """The `last_trigger` of `watchdog.json` for this reaction."""
        rec: dict[str, Any] = {"trigger": self.trigger, "detail": self.detail, "mono_ts": mono, "event_id": event_id}
        if self.signal is not None:
            rec.update(cause=self.signal.cause, source=self.signal.source,
                       launcher_update=self.signal.launcher_update,
                       build_manifest_changed=self.signal.build_manifest_changed)
        return rec


# -- pure helpers ---------------------------------------------------------------------------------------------------
def main_frame(windows: Iterable[WindowInfo], title_regex: str) -> WindowInfo | None:
    """The visible `SunAwtFrame` whose title matches the main-window regex (10.2 sampling 3, 7.3)."""
    pattern = re.compile(title_regex)
    return next((w for w in windows if w.visible and w.cls == FRAME_CLASS and pattern.search(w.title)), None)


def under(path: str | None, folder: str) -> bool:
    """True when `path` lies under `folder`, case-insensitively and with either slash (NTFS paths)."""
    if not path:
        return False

    def norm(p: str) -> str:
        return p.replace("\\", "/").rstrip("/").lower()
    return norm(path).startswith(norm(folder) + "/")


def app_tree(rooted: Iterable[ProcessInfo], foldered: Iterable[ProcessInfo]) -> tuple[ProcessInfo, ...]:
    """The app tree (10.2 sampling 3): the processes descended from the launcher or the main frame (`rooted`, from
    `ProcessPort.process_tree`) plus every process running from the pinned install folder (`foldered`), alive, once
    each, by PID."""
    out: dict[int, ProcessInfo] = {}
    for p in (*rooted, *foldered):
        if p.alive and p.pid not in out:
            out[p.pid] = p
    return tuple(out[k] for k in sorted(out))


def tree_roots(tree: Sequence[ProcessInfo]) -> tuple[int, ...]:
    """The PIDs of the tree whose parent is not in the tree: `ProcessPort.kill_tree` kills each, children first."""
    pids = {p.pid for p in tree}
    return tuple(sorted(p.pid for p in tree if p.ppid not in pids))


def classify_crash_file(path: str) -> CrashSignal | None:
    """A new crash file's signal (10.2 sampling 6): `hs_err_pid*.log` is `jvm_crash`, a heap dump `oom` even while the
    process survives, a WER dump `jvm_crash`; None for anything else."""
    name = Path(path.replace("\\", "/")).name.lower()
    if name.startswith("hs_err_pid") and name.endswith(".log"):
        return CrashSignal("jvm_crash", "crash_file")
    if name.endswith(".hprof"):
        return CrashSignal("oom", "heap_dump")
    if name.endswith((".dmp", ".mdmp", ".hdmp")):
        return CrashSignal("jvm_crash", "wer_dump")
    return None


def region_hash(image: Any, origin: tuple[int, int], rect: tuple[int, int, int, int]) -> str:
    """The sha256 of the main window's pixels in a virtual-screen grab (10.2 sampling 5): `rect` is x, y, w, h in
    virtual-screen pixels, `origin` the grab's position of pixel (0, 0)."""
    x, y, w, h = rect
    ox, oy = origin
    crop = image[max(0, y - oy):max(0, y - oy + h), max(0, x - ox):max(0, x - ox + w)]
    return hashlib.sha256(crop.tobytes() + repr(crop.shape).encode()).hexdigest()


# -- the decision ---------------------------------------------------------------------------------------------------
class Watchdog:
    """The Watchdog's decision state for one shard (module docstring). `restarts_used` seeds the Orchestrator restarts
    already spent (the `resume` lines of an existing journal, `Journal.restarts`)."""

    def __init__(self, config: WatchdogConfig, *, restarts_used: int = 0):
        self.config = config
        self.budgets = ShardBudgets(used_orchestrator=restarts_used)
        self.dead_man_at = dead_man_deadline(config.run_type, config.run_deadline, nightly_cutoff=config.nightly_cutoff)
        self.finished = False                   # the dead-man fired or the shard ships as it is: nothing more to do
        self.self_abort = False
        self.reached_running = False
        self._hash: str | None = None
        self._hash_changed = 0.0
        self._frame_seen: float | None = None   # when a main frame last matched
        self._running_since: float | None = None
        self._launched_since: float | None = None
        self._frame_missing_fired = False
        self._launched_fired = False
        self._orch_baseline: float | None = None   # the last Orchestrator reaction (choice 2)
        self._hook_reported: set[int] = set()
        self._kill_handled: set[tuple[str, str]] = set()
        self._app_seen: set[int] = set()         # app pids whose tree was seen alive (choice 3)
        self._app_failed: int | None = None       # the app pid of the last app-failure reaction
        self._last_pids: frozenset[int] = frozenset()
        self._capture: tuple[str | None, str | None] = (None, None)   # (last_capture_sha256, its first heartbeat ts)
        self._first_ts: str | None = None

    # the trackers every sample updates ---------------------------------------------------------------------------
    def _track(self, now: float, obs: Observation) -> None:
        hb = obs.heartbeat
        if obs.self_abort:
            self.self_abort = True
        if hb is not None:
            self._first_ts = self._first_ts or hb.ts
            if hb.state in RUNNING_STATES:
                self.reached_running = True
            if hb.last_capture_sha256 != self._capture[0]:
                self._capture = (hb.last_capture_sha256, hb.ts)
            self._running_since = (self._running_since if self._running_since is not None else now) \
                if hb.state == "running" else None
            self._launched_since = (self._launched_since if self._launched_since is not None else now) \
                if hb.state == "launched" else None
        if obs.main_frame is not None:
            self._frame_seen = now
            self._frame_missing_fired = False
        if obs.frame_hash is not None and obs.frame_hash != self._hash:
            self._hash, self._hash_changed = obs.frame_hash, now
        if obs.tree:
            self._last_pids = frozenset(p.pid for p in obs.tree)
            if hb is not None and hb.app_pid is not None and hb.app_pid != self._app_failed:
                self._app_seen.add(hb.app_pid)

    def _since(self) -> str | None:
        return self._capture[1] or self._first_ts

    # the sample ----------------------------------------------------------------------------------------------------
    def sample(self, now: float, obs: Observation) -> Reaction | None:
        """One 2 s sample at monotonic `now`: the reaction it calls for, or None (precedence: module docstring)."""
        self._track(now, obs)
        if self.finished:
            return None
        hb = obs.heartbeat
        if obs.wall >= self.dead_man_at and (hb is None or hb.state != "reported"):
            return self._dead_man(obs)
        current = hb is not None and (self._orch_baseline is None or hb.mono_ts > self._orch_baseline)
        dead = current and obs.orchestrator_alive is False
        ks = self._kill_switch(obs, dead)
        if ks is not None:
            return ks
        if not self.self_abort and hb is not None:
            age = now - max(hb.mono_ts, self._orch_baseline if self._orch_baseline is not None else hb.mono_ts)
            if dead:
                return self._orchestrator(now, obs, hb, hung=False)
            if orchestrator_hung(hb.state, age):
                return self._orchestrator(now, obs, hb, hung=True)
        if current and obs.hook_host_alive is False and hb is not None and hb.hook_host_pid is not None \
                and hb.hook_host_pid not in self._hook_reported:
            self._hook_reported.add(hb.hook_host_pid)
            return Reaction("hook_host_dead", (Step("signal", {"code": "hook_host_dead"}),),
                            f"hook host {hb.hook_host_pid} is dead: the Orchestrator fails closed (5.4.2)")
        app = self._app_failure(now, obs)
        if app is not None:
            return app
        return self._frame_rules(now, obs)

    # the triggers ------------------------------------------------------------------------------------------------
    def _evidence(self, obs: Observation, *, screenshot: bool = True, dump: bool = True) -> list[Step]:
        hb = obs.heartbeat
        steps = []
        if screenshot and not (hb is not None and hb.capture_suspended):
            steps.append(Step("screenshot"))
        if dump:
            pid = obs.main_frame.pid if obs.main_frame is not None else (hb.app_pid if hb is not None else None)
            steps.append(Step("thread_dump", {"pid": pid, "timeout_s": THREAD_DUMP_TIMEOUT_S}))
        steps.append(Step("collect", {"since": self._since(), "gateway_events": GATEWAY_EVENTS}))
        return steps

    def _kill(self, obs: Observation, *, orchestrator: int | None = None, hook_host: int | None = None) -> list[Step]:
        steps = [Step("kill_orchestrator", {"pid": orchestrator})] if orchestrator is not None else []
        steps += [Step("reset_input_state"), Step("kill_tree", {"roots": tree_roots(obs.tree)})]
        if hook_host is not None:
            steps.append(Step("kill_hook_host", {"pid": hook_host}))
        return steps

    def _journal(self, action: str, detail: str) -> Step:
        return Step("journal", {"action": action, "detail": detail})

    def _dead_man(self, obs: Observation) -> Reaction:
        hb = obs.heartbeat
        state = hb.state if hb is not None else "provisioned"
        decision = dead_man_finalize(state, self.reached_running, run_type=self.config.run_type)
        argv = (*self.config.resume_argv(), "--finalize", "--state", state) + \
            (("--reached-running",) if self.reached_running else ())
        orch = hb.orchestrator_pid if hb is not None and obs.orchestrator_alive else None
        hook = hb.hook_host_pid if hb is not None else None
        detail = f"finalize: dead-man deadline {_iso(self.dead_man_at)} passed at {state}"
        self.finished = True
        steps = [*self._evidence(obs, dump=False), *self._kill(obs, orchestrator=orch, hook_host=hook),
                 self._journal("resume", detail), Step("start_finalize", {"argv": argv})]
        return Reaction("dead_man", tuple(steps), detail, decision=decision)

    def _kill_switch(self, obs: Observation, dead: bool) -> Reaction | None:
        wires = killflag.tripwires(obs.kill_requests, TripWires(orchestrator_dead=dead, flag_set=obs.ticket_flag))
        trigger = check_tripwires(wires)
        if trigger is None:
            return None
        request = obs.kill_requests.get(trigger)
        key = (trigger, f"{request.at}|{request.requested_by}" if request is not None else "")
        if key in self._kill_handled:
            return None
        self._kill_handled.add(key)
        hb = obs.heartbeat
        gate = hb.state if hb is not None else "running"
        decision = kill_switch(trigger, gate=gate, running_flow=hb.flow_id if hb is not None else None,
                               request=request)
        detail = f"kill switch second half: {trigger}"
        steps = [Step("deny_all"), *self._kill(obs), self._journal("kill_switch", detail)]
        if dead:
            self._orch_baseline = obs.mono
            steps.append(Step("start_resume", {"argv": self.config.resume_argv()}))
        else:
            steps.append(Step("signal", {"code": "kill_switch"}))
        return Reaction("kill_switch", tuple(steps), detail, decision=decision)

    def _orchestrator(self, now: float, obs: Observation, hb: Heartbeat, *, hung: bool) -> Reaction:
        self._orch_baseline = now
        decision = orchestrator_died(self.budgets, step_id=hb.step_id,
                                     state=hb.state if hb.state in ("running", "teardown") else "running")
        trigger = "orchestrator_hung" if hung else "orchestrator_died"
        orch = hb.orchestrator_pid if hung and obs.orchestrator_alive is not False else None
        steps = [*self._evidence(obs), *self._kill(obs, orchestrator=orch, hook_host=hb.hook_host_pid)]
        if decision.has("restart_orchestrator"):
            detail = f"cause: {trigger}; {decision.detail}"
            steps += [self._journal("resume", detail), Step("start_resume", {"argv": self.config.resume_argv()})]
        else:
            self.finished = True
            detail = f"cause: {trigger}; Orchestrator restart budget spent: the shard ships as it is (8.2)"
            steps += [self._journal("watchdog_crash", detail), Step("ship_as_is", {"shard_id": self.config.shard_id})]
        return Reaction(trigger, tuple(steps), detail, decision=decision)

    def _signals(self, now: float, obs: Observation) -> list[CrashSignal]:
        found = [s for s in map(classify_crash_file, obs.crash_files) if s is not None]
        pids = {p.pid for p in obs.tree} | self._last_pids
        for e in obs.event_log:
            if e.event_id in EVENT_LOG_IDS and (e.pid is None or e.pid in pids):
                found.append(CrashSignal("jvm_crash" if e.event_id == 1000 else "hang", "event_log"))
        if obs.log_oom:
            found.append(CrashSignal("oom", "client_log"))
        ceiling = self.config.working_set_ceiling
        if ceiling is not None and sum(p.working_set or 0 for p in obs.tree) > ceiling:
            found.append(CrashSignal("oom", "working_set"))
        if any((p.gdi_objects or 0) >= GDI_LIMIT for p in obs.tree):
            found.append(CrashSignal("hang", "gdi_objects"))
        hb = obs.heartbeat
        if hb is not None and obs.frame_hash is not None and \
                hang_detected(hb, now=now, last_hash_change=self._hash_changed, anti_anchor=obs.anti_anchor):
            found.append(CrashSignal("hang", "screenshot_staleness"))
        if obs.hung_window:
            found.append(CrashSignal("hang", "is_hung_app_window"))
        if obs.jab_stalled:
            found.append(CrashSignal("hang", "jab_timeout"))
        if not obs.tree and hb is not None and hb.app_pid in self._app_seen:
            found.append(CrashSignal("process_exit", "empty_tree", launcher_update=obs.launcher_update,
                                     build_manifest_changed=obs.build_manifest_changed))
        return found

    def _app_failure(self, now: float, obs: Observation) -> Reaction | None:
        hb = obs.heartbeat
        if hb is None or hb.state not in APP_STATES:
            return None
        if self._app_failed is not None and hb.app_pid in (None, self._app_failed):
            return None                          # the reaction killed that app; wait for the relaunch's pid
        sig = crash_signal(self._signals(now, obs))
        if sig is None:
            return None
        self._app_failed = hb.app_pid
        if hb.app_pid is not None:
            self._app_seen.discard(hb.app_pid)
        self._hash, self._hash_changed = None, now
        detail = f"cause: {sig.cause} ({sig.source})"
        steps = [*self._evidence(obs), *self._kill(obs),
                 self._journal("watchdog_hang" if sig.cause == "hang" else "watchdog_crash", detail),
                 Step("signal", {"code": "app_failure"})]
        return Reaction("app_failure", tuple(steps), detail, cause=sig.cause, source=sig.source, signal=sig)

    def _frame_rules(self, now: float, obs: Observation) -> Reaction | None:
        if obs.main_frame is not None:
            return None
        seen = self._frame_seen
        running = self._running_since
        if running is not None and not self._frame_missing_fired and \
                now - max(running, seen if seen is not None else running) >= MAIN_FRAME_MISSING_S:
            self._frame_missing_fired = True
            return Reaction("main_frame_missing", (), "no frame matches the main-window regex for 30 s while running: "
                                                      "the tree is tracked by the install-folder rule (choice 5)")
        if self._launched_since is not None and not self._launched_fired and obs.install_alive and \
                (seen is None or seen < self._launched_since) and now - self._launched_since >= LAUNCHED_FRAME_S:
            self._launched_fired = True
            return Reaction("launched_failed", (Step("signal", {"code": "launched_failed"}),),
                            "no main frame 5 minutes after launch while a process from the install folder is alive: "
                            "the Orchestrator falls back to the Help > About read (7.3)")
        return None


# -- the reverse heartbeat ------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class WatchdogStatus:
    """`watchdog.json` (10.2): pid, monotonic time, last trigger, and the free-space result the Orchestrator reads at
    every flow boundary."""

    pid: int
    mono_ts: float
    ts: str
    last_trigger: Mapping[str, Any] | None = None
    free_bytes: int | None = None


class StatusWriter:
    """Writes `watchdog.json` atomically (temp file, then rename), every sample."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def write(self, status: WatchdogStatus) -> bool:
        """True when written; a failed write is left to the Orchestrator's 30 s rule (`watchdog_dead`)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(asdict(status), sort_keys=True, default=dict), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False


def read_status(path: str | os.PathLike) -> WatchdogStatus | None:
    """`watchdog.json` as the Orchestrator reads it; None when missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return WatchdogStatus(pid=int(data["pid"]), mono_ts=float(data["mono_ts"]), ts=str(data["ts"]),
                              last_trigger=data.get("last_trigger"), free_bytes=data.get("free_bytes"))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def status_check(path: str | os.PathLike, *, now: float, pid_alive: Callable[[int], bool]) -> tuple[float, bool]:
    """The `(age_s, pid_alive)` pair `watchdog.watchdog_check` takes (the `rig.watchdog_status` read): a missing or
    unreadable file is infinitely old with a dead PID."""
    s = read_status(path)
    if s is None:
        return float("inf"), False
    return max(0.0, now - s.mono_ts), pid_alive(s.pid)


def crash_signal_of(status: WatchdogStatus | None) -> CrashSignal | None:
    """The app failure the last trigger reports, as the `CrashSignal` the Orchestrator's `watchdog.app_failure` takes;
    None for any other trigger."""
    t = status.last_trigger if status is not None else None
    if not t or t.get("trigger") != "app_failure":
        return None
    return CrashSignal(str(t["cause"]), str(t["source"]), launcher_update=bool(t.get("launcher_update")),
                       build_manifest_changed=bool(t.get("build_manifest_changed")))


# -- the journal ----------------------------------------------------------------------------------------------------
class JournalUnwritable(OSError):
    """A `watchdog_events.jsonl` append failed (10.2: Event Log 9001 and the named event follow)."""


class Journal:
    """`watchdog_events.jsonl`, append-only, in the line format of `store.journal` (choice 6)."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._next = 1 + max((int(m.group(1)) for line in self._lines()
                              if (m := re.fullmatch(r"wd-(\d+)", str(line.get("event_id", ""))))), default=0)

    def _lines(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue                         # a torn last line: the reader skips it too (store.journal)
        return out

    def restarts(self) -> int:
        """The Orchestrator restarts already journaled: `resume` lines that are not the dead-man's finalize."""
        return sum(1 for e in self._lines()
                   if e.get("action") == "resume" and not str(e.get("detail") or "").startswith("finalize"))

    def entry(self, action: str, *, at: datetime, mono: float, detail: str | None = None,
              heartbeat: Heartbeat | None = None, app_uptime_s: int | None = None,
              app_restart_count: int | None = None) -> dict[str, Any]:
        """The next line (not yet written): a fresh `wd-NNNN` id, the action, the times, and the flow, step and
        checkpoint the heartbeat names."""
        if action not in WATCHDOG_ACTIONS:
            raise ValueError(f"Watchdog action {action!r}; one of {WATCHDOG_ACTIONS} (store.journal)")
        line: dict[str, Any] = {"event_id": f"wd-{self._next:04d}", "action": action, "at": _iso(at),
                                "mono_ms": int(round(mono * 1000))}
        self._next += 1
        if heartbeat is not None:
            line.update({k: v for k, v in (("flow_id", heartbeat.flow_id), ("step_id", heartbeat.step_id),
                                           ("checkpoint_id", heartbeat.checkpoint_id)) if v is not None})
        for k, v in (("detail", detail), ("app_uptime_s", app_uptime_s), ("app_restart_count", app_restart_count)):
            if v is not None:
                line[k] = v
        return line

    def append(self, line: Mapping[str, Any]) -> None:
        """Append one line and flush it to disk; raises `JournalUnwritable`."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(line), sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            raise JournalUnwritable(e.errno, f"watchdog journal append failed: {e}") from e


# -- the host and the reaction run ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class ThreadDump:
    """`jcmd <pid> Thread.print` and `GC.heap_info` (10.2 reaction step 2): `status` one of `THREAD_DUMP_STATUSES`,
    `ref` the stored artifact."""

    status: str
    ref: str | None = None

    def __post_init__(self) -> None:
        if self.status not in THREAD_DUMP_STATUSES:
            raise ValueError(f"thread dump status {self.status!r}")


@dataclass(frozen=True)
class Collection:
    """What reaction step 3 collected, as stored artifact references; `client_log` None when no log directory was
    found."""

    client_log: str | None = None
    hs_err: tuple[str, ...] = ()
    dumps: tuple[str, ...] = ()
    event_log: tuple[str, ...] = ()
    metrics: str | None = None
    heartbeats: tuple[str, ...] = ()
    gateway_events: str | None = None


class WatchdogHost(Protocol):
    """What the Watchdog needs from the rig beyond `DesktopPort` and `ProcessPort` (10.2); the Windows track
    implements it (jcmd, the Event Log, the client log, the hook's segment, the named events, process start)."""

    def processes_in(self, folder: str) -> tuple[ProcessInfo, ...]:
        """Every live process whose image lies under `folder` (the install-folder rule)."""

    def pid_alive(self, pid: int) -> bool:
        """True while the process lives (a handle held per PID, so a reused PID never revives it)."""

    def free_bytes(self) -> int:
        """Free bytes on the artifact volume."""

    def event_log(self, since: str) -> tuple[EventLogEntry, ...]:
        """Application Event Log 1000 and 1002 entries for install-folder processes since `since`."""

    def client_log_oom(self, since: str) -> bool:
        """True when the client log has an `OutOfMemoryError` line since `since`."""

    def version_changed(self, since: str) -> tuple[bool, bool]:
        """(a launcher update in the launcher log since `since`, the build manifest differs from the run's)."""

    def hung(self, hwnd: int) -> bool:
        """`IsHungAppWindow` or `SendMessageTimeout(WM_NULL)` failing (the secondary check)."""

    def anti_anchor(self, frame: Any) -> bool:
        """A spinner, Loading or Reconnecting template on the grab."""

    def ticket_flag(self) -> bool:
        """The hook's order-stage flag."""

    def named_event(self) -> str | None:
        """The Orchestrator's pending named-event code (`self_abort`, `free_space_check`), cleared on read."""

    def screenshot(self) -> str | None:
        """A full virtual-screen screenshot into the restricted folder, redacted before storage; its reference."""

    def thread_dump(self, pid: int, timeout_s: float) -> ThreadDump:
        """`jcmd` from a JDK of the bundled runtime's major version."""

    def collect(self, since: str | None, gateway_events: int) -> Collection:
        """Reaction step 3."""

    def reset_input_state(self) -> None:
        """`reset_input_state` through a fresh gateway instance."""

    def deny_all(self) -> None:
        """Set the hook's deny-all state."""

    def signal(self, code: str) -> None:
        """Raise the named event toward the Orchestrator with `code` (`SIGNALS`)."""

    def start(self, argv: Sequence[str]) -> None:
        """Start the Orchestrator with `argv` (`orchestrator --resume ...`)."""

    def ship_as_is(self, shard_id: str) -> None:
        """Ask the Rig Manager to ship the shard as it is (8.2: the Orchestrator restart budget is spent)."""

    def write_event_log(self, event_id: int, message: str) -> None:
        """An Application Event Log entry, source Sentinel."""


@dataclass(frozen=True)
class Evidence:
    """One item of the evidence manifest: its kind, its stored reference, or the note saying why it is missing."""

    kind: str
    ref: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class ReactionReport:
    """What `run_reaction` did: the evidence manifest, the journal line written (None when the reaction has none or the
    append failed), and the step names in the order run."""

    evidence: tuple[Evidence, ...]
    journal_line: Mapping[str, Any] | None
    ran: tuple[str, ...]
    survivors: bool = False

    @property
    def notes(self) -> tuple[str, ...]:
        return tuple(e.note for e in self.evidence if e.note)

    def manifest(self) -> list[dict[str, Any]]:
        """The manifest as JSON for `watchdog.json` and the `app_crash` finding's artifact list."""
        return [{k: v for k, v in asdict(e).items() if v is not None} for e in self.evidence]


def _collected(c: Collection) -> list[Evidence]:
    out = [Evidence("client_log", c.client_log) if c.client_log
           else Evidence("client_log", note="client log: not located")]
    out += [Evidence("hs_err", r) for r in c.hs_err] + [Evidence("dump", r) for r in c.dumps]
    out += [Evidence("event_log", r) for r in c.event_log] + [Evidence("heartbeat", r) for r in c.heartbeats]
    out += [Evidence(k, r) for k, r in (("metrics", c.metrics), ("gateway_events", c.gateway_events)) if r]
    return out


def run_reaction(reaction: Reaction, *, host: WatchdogHost, processes: ProcessPort, journal: Journal,
                 at: datetime, mono: float, heartbeat: Heartbeat | None) -> ReactionReport:
    """Run the reaction's steps in order (10.2 Reaction) and build the evidence manifest. A thread dump that times out
    or cannot attach is noted, not fatal; a journal append that fails writes Event Log 9001 and raises the named
    event, and the reaction goes on (10.2)."""
    evidence: list[Evidence] = []
    line: Mapping[str, Any] | None = None
    survivors = False
    for step in reaction.steps:
        a = step.args
        if step.name == "screenshot":
            ref = host.screenshot()
            evidence.append(Evidence("screenshot", ref) if ref else Evidence("screenshot", note="screenshot: failed"))
        elif step.name == "thread_dump":
            dump = ThreadDump("no_process") if a.get("pid") is None else host.thread_dump(int(a["pid"]),
                                                                                        float(a["timeout_s"]))
            evidence.append(Evidence("thread_dump", dump.ref) if dump.status == "ok"
                            else Evidence("thread_dump", dump.ref, _UNAVAILABLE[dump.status]))
        elif step.name == "collect":
            evidence += _collected(host.collect(a.get("since"), int(a.get("gateway_events", GATEWAY_EVENTS))))
        elif step.name in ("kill_orchestrator", "kill_hook_host"):
            processes.kill_tree(int(a["pid"]))
        elif step.name == "deny_all":
            host.deny_all()
        elif step.name == "reset_input_state":
            host.reset_input_state()
        elif step.name == "kill_tree":
            for root in a.get("roots", ()):
                if not processes.kill_tree(int(root)):
                    survivors = True
            if survivors:
                evidence.append(Evidence("kill", note="kill: a process from the install folder survived"))
        elif step.name == "journal":
            notes = [e.note for e in evidence if e.note]
            detail = "; ".join([str(a["detail"]), *notes])
            entry = journal.entry(str(a["action"]), at=at, mono=mono, detail=detail, heartbeat=heartbeat)
            try:
                journal.append(entry)
                line = entry
            except JournalUnwritable as e:
                host.write_event_log(JOURNAL_FAILED_EVENT, f"{EVENT_SOURCE} watchdog journal: {e}; lost line {entry}")
                host.signal("journal_failed")
        elif step.name == "signal":
            host.signal(str(a["code"]))
        elif step.name in ("start_resume", "start_finalize"):
            host.start(tuple(a["argv"]))
        elif step.name == "ship_as_is":
            host.ship_as_is(str(a["shard_id"]))
    return ReactionReport(tuple(evidence), line, reaction.names, survivors)


# -- the process ----------------------------------------------------------------------------------------------------
def _read_heartbeat(path: Path) -> Heartbeat | None:
    try:
        return Heartbeat(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


class WatchdogLoop:
    """The Watchdog process for one shard: every 2 s, observe, decide, react and write `watchdog.json`."""

    def __init__(self, config: WatchdogConfig, *, desktop: DesktopPort, processes: ProcessPort, host: WatchdogHost,
                 clock: Clock, kill_flag: KillChannel | None = None, kill_pipe: KillChannel | None = None,
                 pid: int | None = None):
        self.config = config
        shard = Path(config.shard_dir)
        self.heartbeat_path = shard / HEARTBEAT_FILE
        self.journal = Journal(shard / WATCHDOG_JOURNAL)
        self.status = StatusWriter(shard / STATUS_FILE)
        self.watchdog = Watchdog(config, restarts_used=self.journal.restarts())
        self.desktop, self.processes, self.host, self.clock = desktop, processes, host, clock
        self.kill_flag, self.kill_pipe = kill_flag, kill_pipe
        self.pid = os.getpid() if pid is None else pid
        self.last_trigger: dict[str, Any] | None = None
        self._since = _iso(clock.now())

    def observe(self) -> Observation:
        """One sample's reads (module docstring): the tree before the heartbeat (choice 3), no grab while
        `capture_suspended`."""
        cfg, host = self.config, self.host
        mono, wall = self.clock.monotonic(), self.clock.now()
        since, self._since = self._since, _iso(wall)
        frame = main_frame(self.desktop.enumerate_windows(), cfg.title_regex)
        roots = {p for p in (cfg.launcher_pid, frame.pid if frame else None) if p is not None}
        foldered = host.processes_in(cfg.install_dir)
        tree = app_tree([p for r in sorted(roots) for p in self.processes.process_tree(r)], foldered)
        hb = _read_heartbeat(self.heartbeat_path)
        frame_hash, anti = None, False
        if frame is not None and getattr(frame, "rect", None) and hb is not None and not hb.capture_suspended:
            grab = self.desktop.grab()
            frame_hash, anti = region_hash(grab.image, grab.origin, frame.rect), host.anti_anchor(grab)
        update, manifest = host.version_changed(since) if not tree else (False, False)
        code = host.named_event()
        return Observation(
            mono=mono, wall=wall, heartbeat=hb,
            orchestrator_alive=host.pid_alive(hb.orchestrator_pid) if hb and hb.orchestrator_pid else None,
            hook_host_alive=host.pid_alive(hb.hook_host_pid) if hb and hb.hook_host_pid else None,
            main_frame=frame, tree=tree, install_alive=any(p.alive for p in foldered), frame_hash=frame_hash,
            anti_anchor=anti, hung_window=host.hung(frame.hwnd) if frame is not None else False,
            crash_files=self.processes.crash_files(since), event_log=host.event_log(since),
            log_oom=host.client_log_oom(since), launcher_update=update, build_manifest_changed=manifest,
            free_bytes=host.free_bytes(), self_abort=code == "self_abort",
            kill_requests=killflag.pending(self.kill_flag, self.kill_pipe), ticket_flag=host.ticket_flag())

    def tick(self) -> tuple[Reaction | None, ReactionReport | None]:
        """One sample: observe, decide, react, then write `watchdog.json` (10.2)."""
        obs = self.observe()
        reaction = self.watchdog.sample(obs.mono, obs)
        report = None
        if reaction is not None:
            report = run_reaction(reaction, host=self.host, processes=self.processes, journal=self.journal,
                                  at=obs.wall, mono=obs.mono, heartbeat=obs.heartbeat) if reaction.steps else None
            event = report.journal_line["event_id"] if report and report.journal_line else None
            self.last_trigger = {**reaction.trigger_record(event, obs.mono),
                                 **({"evidence": report.manifest()} if report else {})}
        self.status.write(WatchdogStatus(self.pid, obs.mono, _iso(obs.wall), self.last_trigger, obs.free_bytes))
        return reaction, report

    def run(self, stop: threading.Event, *, interval_s: float = SAMPLE_S) -> None:
        """Sample every `interval_s` until `stop` is set."""
        while not stop.is_set():
            self.tick()
            self.clock.sleep(interval_s)
