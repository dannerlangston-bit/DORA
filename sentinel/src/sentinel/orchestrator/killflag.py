r"""The kill switch's human channels: the file flag, the named pipe, `sentinel stop <rig>` and preflight H5 (6.6
Triggers, 5.2 kill-switch poll, 12.7 H5).

Two human channels reach the Orchestrator and the Watchdog on the rig (6.6):

- the file flag at the fixed path `FLAG_PATH`, written by the controller command `sentinel stop <rig>` over the
  hypervisor guest channel or by the Rig Manager; the Orchestrator polls it every 500 ms (`POLL_S`, 5.2) and the
  Watchdog on its 2 s sample (10.2);
- the named pipe `PIPE_NAME`, for processes on the rig (Watchdog, Orchestrator, drill injector).

Each is a `ports.KillChannel`. `FileFlagChannel` is the file flag over any path: the rig's own `FLAG_PATH`, or in
the simulation a directory per rig, where it is also the transport `sentinel stop` writes through (`rig_flag_path`).
`MemoryKillChannel` is the in-process stand-in for the named pipe and the guest channel, whose Win32 and hypervisor
transports belong to the Windows track.

- `poll(channel)` is the pending human request on one channel, or None; `pending(flag, pipe)` reads both and
  `tripwires(requests, wires)` sets `TripWires.file_flag` and `named_pipe` from them. `check(flag, pipe, gate=...)`
  is the whole boundary test (the Orchestrator at every step and flow boundary, the Watchdog on its sample): the
  first trigger by `killswitch.check_tripwires`, then `killswitch.kill_switch` with the request that fired it, so
  the run is `aborted_safety (reason: kill_switch)` and every unreached checkpoint `env_error (gate: <gate>, reason:
  kill_switch)` (SD-59). `acknowledge(flag, pipe)` marks the requests handled after the sequence.
- `h5_round_trip(flag, pipe, rig_id=..., clock=...)` is preflight H5, "File flag write and clear; named pipe round
  trip", as the `ports.PreflightRead` of item H5; `h5_passed(read)` is its pass rule, "Both work".
- `main(argv)` is `sentinel stop <rig> [--reason TEXT] [--by NAME] [--root DIR]`.

Choices where the spec is silent (reported):

1. The fixed path is `C:\sentinel\run\kill.flag`, beside the shard directories of 8.4, and the pipe is
   `\\.\pipe\sentinel-kill`. In the simulation a rig's root is `<root>/<rig>`, so its flag is
   `<root>/<rig>/run/kill.flag`.
2. The flag file holds the request as JSON (who, through which channel, why, when; `ports.KillRequest`). A flag that
   is present but cannot be read or parsed, and a channel that raises when polled, count as a stop request: a
   reader the switch depends on fails closed (6, preamble).
3. H5 writes a probe request (channel `preflight`, reason `preflight.H5`) that `poll` does not count, so a Watchdog
   sampling during preflight does not trip; `sentinel stop` always writes channel `cli` and refuses that reason, so a
   human request is never taken for a probe. A real request already pending fails H5 and is left in place, never
   overwritten: the run does not start while a stop is pending.
4. `acknowledge` runs after the sequence's revert (6.6 step 6), which clears the flag on a real rig anyway, and
   removes only a request it read: one written in between stays pending.
5. The harness's own writes never overwrite a human request: fire drill item 5 (`drill_request`) and the H5 probe
   are posted to the file flag with an exclusive create, which a stale probe gives way to and any other request
   refuses (FileExistsError), while a human request (`sentinel stop`) replaces whatever is there; `sentinel stop`
   refuses the drill's own name and reason as it refuses the probe's, so a human request is never taken for either.
   Item 5 also polls first and leaves a pending request in place (drill.py), and the Orchestrator polls the human
   channels once more when the work returns, so a stop written during the drill ends the run `aborted_safety
   (reason: kill_switch)` (6.6 Triggers).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path, PurePath, PureWindowsPath

from .clock import Clock, SystemClock
from .killswitch import TripWires, check_tripwires, kill_switch
from .ports import KillChannel, KillRequest, PreflightRead
from .results import ControlResult

POLL_S = 0.5                                     # 5.2: the Orchestrator polls the file flag every 500 ms
RIG_ROOT = PureWindowsPath("C:/sentinel")
PIPE_NAME = r"\\.\pipe\sentinel-kill"
PROBE_CHANNEL = "preflight"
PROBE_REASON = "preflight.H5"
DRILL_BY = "fire_drill"                          # fire drill item 5's own request (choice 5)
DRILL_REASON = "fire drill item 5"
TRIGGERS = ("file_flag", "named_pipe")            # killswitch.check_tripwires names of the two human channels


def flag_path(rig_root: PurePath) -> PurePath:
    """The file flag under a rig's root (choice 1)."""
    return rig_root / "run" / "kill.flag"


FLAG_PATH = flag_path(RIG_ROOT)                   # C:\sentinel\run\kill.flag, the fixed path on the rig (6.6)


def rig_flag_path(root: Path, rig_id: str) -> Path:
    """A simulated rig's file flag: its root is `<root>/<rig_id>` (choice 1)."""
    return Path(flag_path(root / rig_id))


def is_probe(request: KillRequest) -> bool:
    """True for the H5 probe, which is never a stop request (choice 3)."""
    return request.channel == PROBE_CHANNEL and request.reason == PROBE_REASON


def drill_request(rig_id: str, at: str) -> KillRequest:
    """The request fire drill item 5 writes (6.5): a stop request, but the harness's own (choice 5)."""
    return KillRequest(rig_id, "file", DRILL_BY, DRILL_REASON, at)


def is_drill(request: KillRequest) -> bool:
    """True for fire drill item 5's own request (choice 5)."""
    return request.requested_by == DRILL_BY and request.reason == DRILL_REASON


class FileFlagChannel:
    """The file flag as a `ports.KillChannel` (choice 2): `post` writes the request atomically, `poll` reads it,
    `acknowledge` removes it if it still holds that request."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def post(self, request: KillRequest) -> None:
        """Write `request` atomically. A human request replaces whatever is there; the harness's own (the H5 probe,
        drill item 5's) is written only where nothing but a stale probe is, and raises FileExistsError over any other
        request, so it never overwrites a human stop (choice 5)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(request), sort_keys=True), encoding="utf-8")
        try:
            if is_probe(request) or is_drill(request):
                try:
                    os.link(tmp, self.path)              # an exclusive create: refused when a flag exists
                    return
                except FileExistsError:
                    held = self.poll()
                    if held is not None and not is_probe(held):
                        raise FileExistsError(f"a stop request is pending in {self.path}") from None
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def poll(self) -> KillRequest | None:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            return _unreadable(f"kill flag {self.path} unreadable: {e}")
        try:                                     # bytes that are not UTF-8 (UnicodeDecodeError) are no request either
            return KillRequest(**json.loads(data.decode("utf-8")))
        except (ValueError, TypeError) as e:
            return _unreadable(f"kill flag {self.path} not a request: {e}")

    def acknowledge(self, request: KillRequest) -> None:
        if self.poll() == request:
            self.path.unlink(missing_ok=True)


class MemoryKillChannel:
    """An in-process `ports.KillChannel`, first in first out: the simulated named pipe and guest channel."""

    def __init__(self) -> None:
        self.requests: list[KillRequest] = []

    def post(self, request: KillRequest) -> None:
        self.requests.append(request)

    def poll(self) -> KillRequest | None:
        return self.requests[0] if self.requests else None

    def acknowledge(self, request: KillRequest) -> None:
        if request in self.requests:
            self.requests.remove(request)


def _unreadable(why: str) -> KillRequest:
    """The stop request a channel that cannot be read stands for (choice 2)."""
    return KillRequest(rig_id="unknown", channel="unreadable", requested_by="unknown", reason=why, at="")


def poll(channel: KillChannel) -> KillRequest | None:
    """The pending human stop request on one channel, the H5 probe excluded; a channel that raises fails closed."""
    try:
        request = channel.poll()
    except (OSError, ValueError) as e:
        return _unreadable(f"kill channel unreadable: {e}")
    return None if request is None or is_probe(request) else request


def pending(flag: KillChannel | None, pipe: KillChannel | None = None) -> dict[str, KillRequest]:
    """The pending requests by the trigger they fire (`file_flag`, `named_pipe`)."""
    out = {}
    for trigger, channel in zip(TRIGGERS, (flag, pipe)):
        request = poll(channel) if channel is not None else None
        if request is not None:
            out[trigger] = request
    return out


def tripwires(requests: Mapping[str, KillRequest], wires: TripWires = TripWires()) -> TripWires:
    """`wires` (the automatic trip-wires of the same sample) with the human channels set from `requests`."""
    return replace(wires, file_flag=wires.file_flag or "file_flag" in requests,
                   named_pipe=wires.named_pipe or "named_pipe" in requests)


def check(flag: KillChannel | None, pipe: KillChannel | None = None, *, gate: str, running_flow: str | None = None,
          wires: TripWires = TripWires(), drop_limit: int = 10) -> ControlResult | None:
    """One kill-switch test at a step or flow boundary or a Watchdog sample (6.6 Triggers): the kill switch decision
    for the first trigger that fires, human or automatic, with the request behind a human trigger; None otherwise."""
    requests = pending(flag, pipe)
    trigger = check_tripwires(tripwires(requests, wires), drop_limit=drop_limit)
    if trigger is None:
        return None
    return kill_switch(trigger, gate=gate, running_flow=running_flow, request=requests.get(trigger))


def acknowledge(flag: KillChannel | None, pipe: KillChannel | None = None) -> list[KillRequest]:
    """Mark every pending human request handled, after the sequence's revert (choice 4); returns them."""
    done = []
    for channel in (flag, pipe):
        request = poll(channel) if channel is not None else None
        if channel is not None and request is not None:
            channel.acknowledge(request)
            done.append(request)
    return done


# -- preflight H5 ---------------------------------------------------------------------------------------------------
def _round_trip(channel: KillChannel, rig_id: str, clock: Clock) -> str | None:
    """Write a probe, read it back, clear it, see it gone; None when that worked, else what failed."""
    probe = KillRequest(rig_id, PROBE_CHANNEL, "sentinel", PROBE_REASON, clock.now().isoformat())
    try:
        before = channel.poll()
        if before is not None and not is_probe(before):
            return f"a stop request is pending ({before.requested_by} via {before.channel}: {before.reason})"
        if before is not None:
            channel.acknowledge(before)                  # a probe an interrupted H5 left behind
        channel.post(probe)
        if channel.poll() != probe:
            return "the request written did not read back"
        channel.acknowledge(probe)
        if channel.poll() is not None:
            return "the request did not clear"
    except (OSError, ValueError) as e:
        return f"{type(e).__name__}: {e}"
    return None


def h5_round_trip(flag: KillChannel, pipe: KillChannel, *, rig_id: str, clock: Clock) -> PreflightRead:
    """Preflight H5 (12.7): the file flag written and cleared, the named pipe round trip; values `file_flag` and
    `named_pipe` are True for a channel that worked, and `detail` says what failed."""
    problems = {name: _round_trip(channel, rig_id, clock) for name, channel in zip(TRIGGERS, (flag, pipe))}
    detail = "; ".join(f"{name}: {why}" for name, why in problems.items() if why) or None
    return PreflightRead("H5", {name: why is None for name, why in problems.items()}, readable=True, detail=detail)


def h5_passed(read: PreflightRead) -> bool:
    """The H5 pass rule, "Both work" (12.7); a failure is `aborted_harness (reason: preflight.H5)`."""
    return read.item == "H5" and read.readable and all(read.values.get(name) is True for name in TRIGGERS)


# -- sentinel stop <rig> --------------------------------------------------------------------------------------------
def _user() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


def main(argv: Sequence[str] | None = None, *, transport: Callable[[str], KillChannel] | None = None,
         clock: Clock | None = None) -> int:
    """`sentinel stop <rig>`: write the rig's file flag (6.6). The transport is `transport(rig)` (the hypervisor
    guest channel on the Windows track), or with `--root` the simulated rig's flag file. Returns 0 when the request
    was written, 1 when the transport failed, 2 on a usage error."""
    ap = argparse.ArgumentParser(prog="sentinel stop", description="Trip the kill switch on a rig: write its file "
                                 "flag; the run ends aborted_safety (kill_switch) and the 6.6 sequence runs.")
    ap.add_argument("rig", help="the rig id, for example rig-01")
    ap.add_argument("--reason", default="operator stop", help="why the run is stopped (kept on the run record)")
    ap.add_argument("--by", default=None, help="who asks (default: the logged-in user)")
    ap.add_argument("--root", default=None, help="simulated rigs: the directory that holds <rig>/run/kill.flag")
    args = ap.parse_args(argv)
    if transport is None and args.root is None:
        print("sentinel stop: no transport to the rig; pass --root DIR for simulated rigs", file=sys.stderr)
        return 2
    if args.reason == PROBE_REASON:
        print(f"sentinel stop: {PROBE_REASON!r} is reserved for the preflight probe", file=sys.stderr)
        return 2
    if args.by == DRILL_BY and args.reason == DRILL_REASON:
        print(f"sentinel stop: {DRILL_REASON!r} by {DRILL_BY!r} is reserved for the fire drill", file=sys.stderr)
        return 2
    request = KillRequest(args.rig, "cli", args.by or _user(), args.reason,
                          (clock or SystemClock()).now().isoformat())
    channel = transport(args.rig) if transport is not None else FileFlagChannel(rig_flag_path(Path(args.root),
                                                                                              args.rig))
    try:
        channel.post(request)
    except OSError as e:
        print(f"sentinel stop: could not write the kill flag on {args.rig}: {e}", file=sys.stderr)
        return 1
    where = f" ({channel.path})" if isinstance(channel, FileFlagChannel) else ""
    print(f"kill switch requested on {args.rig}{where}: {args.reason}")
    return 0
