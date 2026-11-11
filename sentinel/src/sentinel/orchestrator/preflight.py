"""The 12.7 preflight items evaluated from raw reads, with the one retry rule (5.3, 7.1, 7.11, 10.8, 12.7).

The rig reads each item through `ports.PreflightReads` (registry values, display metrics, process lists, latency
samples, probe frames, the hook's drop list); this module decides, never the rig:

- `evaluate(item, read, pins) -> ItemResult`: one pure evaluator per item comparing the read with the pinned values of
  `PreflightPins` (the snapshot manifest, the pipeline record, the calibration fingerprint, the corpus entries, the
  rendered vmoptions template). A read that is unreadable, lacks a value or has nothing pinned to compare with fails:
  nothing passes by default (12.7).
- `run_preflight(reads_fn, clock, pins, run_id=...) -> PreflightRun`: every item once, then one 60 s wait on the
  injected clock and a second attempt of each failed item (7.11, 12.7); both attempts are `records.PreflightAttempt`
  rows for run health, and `report` is the `rig.PreflightReport` that `gates.check_provisioned` takes, with the
  logged harness DPI awareness in `dpi_awareness` (gates choice 6).
- Controller-side reads (12.7 G1, G3, G4, G5; the control-host half of R7 and the config checks of H7): `g1_read`,
  `g3_read` with `catalog_versions` (the run row's `catalog_versions_json`), `g4_read` over a calendar callable,
  `g5_read` over `fingerprint.g5`, and `combine` / `merge_reads` to put them beside the rig's reads.
- `h4_expected_drops()`: the drops H4 must see, computed by the real hook decision code (`gateway.hook_decide` and
  `gateway.drain`) on the self-test's two events, so the self-test and the gateway cannot disagree.
- `frame_uniform(image)` and `probe_pattern()`: the H3 and R6 probe analysis.
- `SimulatedPreflightReads(pins, vmoptions=..., overrides=...)`: a `ports.PreflightReads` whose reads pass against the
  pins unless scripted otherwise, for the simulated rig and the demo.

Choices where the spec is ambiguous or silent (reported):

1. Retry (7.11): every item runs once in checklist order; when any failed, the clock waits 60 s once and each failed
   item is read again, so each retry comes at least 60 s after its first attempt. The second attempt decides.
2. `report.detail` follows `gates.check_provisioned`: for an H item it holds only the blocking control (EDR,
   AppLocker, DLP, proxy) when the read named one, because check_provisioned turns it into a hardening finding; for
   G5 it is `fingerprint.g5`'s result (`fingerprint_mismatch:<fields>` or `no_calibration`); for every other item the
   failure causes. Every cause of every attempt is in `attempts` and `run_health()`.
3. H2 latency (10.8, 12.7 H2): at least 1,000 callback latencies, the worst under 20 ms (the hook timeout) and the
   p99.9 (nearest rank) under 1 ms. 10.8 writes the statistics on its H1 and H2 row; they belong to the hook, so H2
   carries them. The Recorder's image names are `PreflightPins.recorder_images` (default `sentinel-recorder.exe`,
   until packaging names it).
4. H3 (10.8): the probe window's capture equals `probe_pattern()` byte for byte (the harness paints that pattern) and
   the desktop probe is not uniform (`frame_uniform`: every pixel one color). The DPI awareness is logged and returned
   as `PreflightRun.dpi_awareness`; a wrong value also fails H3, and check_provisioned turns it into
   `aborted_harness (reason: dpi_awareness)`.
5. R3 compares the ClearType read with `PreflightPins.cleartype`, the calibration fingerprint's block; before any
   calibration exists for the rendering key the caller passes the snapshot manifest's block (G5 then queues a
   Calibration, 7.13). Smoothing on with ClearType type is required either way (7.1).
6. B3 compares the vmoptions hash, the JRE release hash and the pinned Java2D pipeline with every reference given in
   `PreflightPins.b3_references` (`manifest` required; `pair` and `calibration` when known). H10 checks the written
   vmoptions text: its sha256 equals the rendered template (`PreflightPins.vmoptions_sha256`) and the crash flags are
   present (`rigfiles.validate_vmoptions`, naming a missing flag), the crash folders writable and the WER LocalDumps
   key present.
7. Values the evaluators read (the 12.7 Method column, as raw values): R1 `manifest_sha256`, `current_build`, `ubr`,
   `pending_restart`; R2 `width`, `height` (EnumDisplaySettings), `cx`, `cy` (GetSystemMetrics), `bits`, `frequency`,
   `dpi`, `monitors`, `awareness` (of the reading process); R3 `smoothing`, `type`, `gamma`, `contrast`,
   `orientation`; R4 `time_zone`, `layout`, `layout_count`, `caps`, `num`, `scroll`, `locale`, `decimal`, `thousands`,
   `short_date`; R5 the `R5_PINS` keys; R6 `input_desktop`, `console`, `active`, `rdp`, `locked`, `foreground_hwnd`,
   `probe`; R7 `rig_free_bytes`, `shard_bytes`, `control_free_bytes`, `run_bytes`, `ntp_offset_s`; B1 `artifacts`
   (role to sha256); B2 `auto_update_disabled` (Branch A) or `snapshot_build_id` (Branch B); B3 `vmoptions_sha256`,
   `jre_release_sha256`, `java2d_pinned`; H1 `click_received`, `chord_received`; H2 `installed`, `event_received`,
   `removed`, `latencies_ms`, `processes`; H3 `probe`, `desktop`, `awareness`; H4 `drops`; H5 `file_flag`,
   `named_pipe` (`killflag.h5_round_trip`); H6 `masked`, `egress_logged` (`capture.h6_self_test`); H7 the
   `store.preflight` keys plus `config_commit_ok`, `lint_hash_ok`; H8 `watchdog_pid`, `orchestrator_pid`,
   `watchdog_alive`, `watchdog_age_s`, `heartbeat_read_back`; H9 `labels` (expected to read) and `jab_answers`
   (Branch A); H10 `vmoptions`, `error_dir_writable`, `dump_dir_writable`, `wer_localdumps`; N1 `hosts` (host to
   `reachable`, `chain_trusted`); N2 `models` (id to bool), `message_ok`, `agent_ok`, `cache_read_tokens` (of the
   second identical request); N3 `quote_s`; N4 `healthy` and `dataset_id` (Branch A) or `ondemand_available`
   (Branch B); N5 `accepted`, `bytes`; N6 `reachable`, `unresolved`; N7 `unreachable`; G1 `lint_errors`,
   `commit_ok`, `lint_hash_ok`, `ruling_ok`, `templates_ok`; G2 `files` (corpus id to sha256 or None); G3 `catalogs`
   (path to sha256 hex or None); G4 `unresolved`, `list_24h_sha256`; G5 `result`. Any read may add
   `blocking_control` and `screenshot_sha256` (10.8: every failure names the item and attaches a screenshot).
8. R4's en-US formats are the Windows defaults: decimal `.`, thousands `,`, short date `M/d/yyyy`. N5's payload is at
   least 2 MB (2 * 1024**2 bytes). G4 needs the 24-hour list hash read for the epoch (7.10).
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from ..capture import dpi_self_test
from ..lint.rules import PHASE1_DISPLAY, RIG_SPEC_PINS
from ..records import PreflightAttempt
from ..rigfiles import CRASH_FLAGS, validate_vmoptions, vmoptions_sha256
from ..store.preflight import r7_failures, rig_floor_bytes
from .clock import Clock
from .gateway import VK_RETURN, Flag, GatewayState, InputEvent, drain, hook_decide
from .gates import PREFLIGHT_ITEMS
from .killflag import h5_passed
from .ports import PreflightRead
from .rig import PreflightReport

RETRY_WAIT_S = 60.0                          # 7.11, 12.7
H2_MIN_EVENTS = 1000                         # 10.8
H2_P999_MAX_MS = 1.0
HOOK_TIMEOUT_MS = 20.0                       # 5.4, 12.7 H2
WATCHDOG_JSON_MAX_AGE_S = 10.0               # 12.7 H8
QUOTE_MAX_S = 5.0                            # 12.7 N3
EGRESS_PROBE_BYTES = 2 * 1024 ** 2           # 12.7 N5, choice 8
EXPECTED_AWARENESS = "PER_MONITOR_AWARE_V2"  # 7.1
CONTROLLER_ITEMS = ("G1", "G3", "G4", "G5")  # computed by the harness from config and records, not read on Windows
MERGED_ITEMS = ("R7", "H7")                  # a rig read plus a harness-side half
G4_HORIZON_DAYS = 90                         # 12.7 G4
CATALOGS = ("popups/catalog.yaml", "canaries/mutants.yaml", "quarantine.yaml")   # 12.7 G3
R4_FORMATS = {"decimal": ".", "thousands": ",", "short_date": "M/d/yyyy"}         # choice 8
R5_PINS: dict[str, Any] = {"sleep_never": True, "display_never_off": True, "hibernate": False, "screensaver": False,
                           "lock_screen": False, "lock_window": False, "updates_deferred": True,
                           "auto_restart": False, "pending_restart": False, "focus_assist": "alarms_only",
                           "toasts": False}


# -- results ----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ItemResult:
    """One item's verdict: passed, or the causes it failed; the blocking control and screenshot the read named."""

    item: str
    passed: bool
    causes: tuple[str, ...] = ()
    control: str | None = None
    screenshot_sha256: str | None = None

    @property
    def detail(self) -> str | None:
        return "; ".join(self.causes) or None


@dataclass(frozen=True)
class PreflightPins:
    """What the items compare their reads with (12.7 Pass column); a pin an item needs left unset fails the item."""

    snapshot_manifest_sha256: str | None = None                  # R1: the pinned snapshot for this pair (7.2)
    windows_build: Mapping[str, str] | None = None               # R1: {current_build, ubr} of the snapshot manifest
    display: Mapping[str, int] = field(default_factory=lambda: dict(PHASE1_DISPLAY))   # R2: configs/allowed.yaml
    cleartype: Mapping[str, Any] | None = None                   # R3: choice 5
    artifacts: Mapping[str, str] = field(default_factory=dict)   # B1: role -> sha256 of the pipeline record (4.2)
    install_branch: str = "A"                                    # B2: DR-04
    builds: tuple[str, ...] = ()                                 # B2 Branch B: the pair's build ids
    b3_references: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)       # B3: choice 6
    recorder_images: tuple[str, ...] = ("sentinel-recorder.exe",)                      # H2: choice 3
    jab_branch: str = "A"                                        # H9: DR-01
    rig_kind: str = "vm"                                         # H10: 7.1
    jdk_major: int | None = None                                 # H10: the bundled runtime's major (7.3)
    vmoptions_sha256: str | None = None                          # H10: sha256 of the rendered template
    n1_hosts: tuple[str, ...] = ()                               # N1: login, update and market-data hosts
    model_ids: tuple[str, ...] = ()                              # N2: the pinned model ids (3.15)
    data_fixture_branch: str = "A"                               # N4: DR-02
    dataset_id: str | None = None                                # N4 Branch A
    corpus: Mapping[str, str] = field(default_factory=dict)      # G2: corpus id -> file sha256
    catalogs: Mapping[str, str] = field(default_factory=dict)    # G3: path -> sha256 hex at the config commit


class _Missing(Exception):
    """A value the evaluator needs is not in the read."""


def _v(values: Mapping[str, Any], key: str) -> Any:
    if key not in values or values[key] is None:
        raise _Missing(key)
    return values[key]


def _want(out: list[str], name: str, have: Any, want: Any) -> None:
    if have != want:
        out.append(f"{name} is {have!r}, pinned {want!r}")


def _awareness_ok(value: Any) -> bool:
    return dpi_self_test(value if isinstance(value, str) else None) is None


# -- probe frames (H3, R6) --------------------------------------------------------------------------------------
def frame_uniform(image: Any) -> bool:
    """True when every pixel of the frame has one color (a locked or headless console, 10.8)."""
    a = np.asarray(image)
    if a.size == 0:
        return True
    flat = a.reshape(-1, a.shape[-1]) if a.ndim == 3 else a.reshape(-1, 1)
    return bool((flat == flat[0]).all())


def probe_pattern(width: int = 64, height: int = 32) -> np.ndarray:
    """The known pattern the harness probe window paints (H3, 10.8): 8 px cells cycling six colors, BGR uint8."""
    colors = np.array([[0, 0, 255], [0, 255, 0], [255, 0, 0], [0, 255, 255], [255, 0, 255], [255, 255, 0]],
                      dtype=np.uint8)
    yy, xx = np.mgrid[0:height, 0:width]
    return colors[((xx // 8) + (yy // 8) * 3) % len(colors)]


# -- H4 (12.7, 5.3) ---------------------------------------------------------------------------------------------
def h4_expected_drops(now: float = 0.0) -> tuple[str, ...]:
    """The drops the H4 self-test must see, by the real decision code: the probe window flagged as a ticket, an
    injected Enter and an untokened click, each through `hook_decide` and logged through `drain`."""
    state = GatewayState(armed=True)
    state.stamp_watcher(Flag.SET, now)
    events = (InputEvent("key_down", vk=VK_RETURN, origin="foreign_process"),
              InputEvent("button_down", x=10, y=10, button="left", origin="foreign_process"))
    out = []
    for e in events:
        d = hook_decide(state, e, now)
        if d.allowed:
            raise AssertionError(f"the gateway let the H4 event {e.type} through")
        out.append(drain(d, e).reason)
    return tuple(out)


# -- the evaluators ---------------------------------------------------------------------------------------------
Evaluator = Callable[[Mapping[str, Any], PreflightPins], list[str]]


def _r1(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    if p.snapshot_manifest_sha256 is None or p.windows_build is None:
        return ["no pinned snapshot manifest for this pair (7.2)"]
    if _v(v, "manifest_sha256") != p.snapshot_manifest_sha256:
        out.append(f"manifest: snapshot manifest {v['manifest_sha256']} is not the pinned "
                   f"{p.snapshot_manifest_sha256}")
    build = (str(_v(v, "current_build")), str(_v(v, "ubr")))
    pinned = (str(p.windows_build.get("current_build")), str(p.windows_build.get("ubr")))
    if build != pinned:
        out.append(f"windows_build: Windows build {'.'.join(build)} is not the manifest's {'.'.join(pinned)}")
    if _v(v, "pending_restart") is not False:
        out.append("pending_restart: a restart is pending")
    return out


def _r2(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    if not _awareness_ok(_v(v, "awareness")):
        out.append(f"read from a process with awareness {v['awareness']!r}, not {EXPECTED_AWARENESS}")
    w, h, scale = p.display.get("width"), p.display.get("height"), p.display.get("scale_percent")
    _want(out, "EnumDisplaySettings size", (_v(v, "width"), _v(v, "height")), (w, h))
    _want(out, "GetSystemMetrics size", (_v(v, "cx"), _v(v, "cy")), (w, h))
    _want(out, "bits", _v(v, "bits"), RIG_SPEC_PINS["display.bits"])
    _want(out, "frequency", _v(v, "frequency"), RIG_SPEC_PINS["display.frequency"])
    _want(out, "GetDpiForSystem", _v(v, "dpi"), round(96 * scale / 100) if isinstance(scale, int) else None)
    _want(out, "monitors", _v(v, "monitors"), RIG_SPEC_PINS["display.monitors"])
    return out


def _r3(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    _want(out, "smoothing", _v(v, "smoothing"), RIG_SPEC_PINS["cleartype.smoothing"])
    _want(out, "type", _v(v, "type"), RIG_SPEC_PINS["cleartype.type"])
    if p.cleartype is None:
        return out + ["no calibration or manifest ClearType block to compare with (choice 5)"]
    for key in ("gamma", "contrast", "orientation"):
        _want(out, key, _v(v, key), p.cleartype.get(key))
    return out


def _r4(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    _want(out, "time zone", _v(v, "time_zone"), RIG_SPEC_PINS["time_zone.os"])
    _want(out, "keyboard layout", _v(v, "layout"), RIG_SPEC_PINS["keyboard.layout"])
    _want(out, "layout count", _v(v, "layout_count"), RIG_SPEC_PINS["keyboard.layout_count"])
    for key in ("caps", "num", "scroll"):
        _want(out, f"{key} lock", _v(v, key), RIG_SPEC_PINS[f"keyboard.toggle_keys.{key}"])
    _want(out, "locale", _v(v, "locale"), RIG_SPEC_PINS["keyboard.locale"])
    for key, want in R4_FORMATS.items():
        _want(out, key, _v(v, key), want)
    return out


def _r5(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    for key, want in R5_PINS.items():
        _want(out, key, _v(v, key), want)
    return out


def _r6(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    _want(out, "input desktop", _v(v, "input_desktop"), RIG_SPEC_PINS["input_desktop"])
    _want(out, "console session", _v(v, "console"), True)
    _want(out, "session active", _v(v, "active"), True)
    _want(out, "RDP-backed", _v(v, "rdp"), False)
    _want(out, "locked", _v(v, "locked"), False)
    if not v.get("foreground_hwnd"):
        out.append("GetForegroundWindow returned null")
    if frame_uniform(_v(v, "probe")):
        out.append("the probe screenshot is uniform")
    return out


def _r7(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    return list(r7_failures(rig_free_bytes=_v(v, "rig_free_bytes"), shard_bytes=_v(v, "shard_bytes"),
                            control_free_bytes=_v(v, "control_free_bytes"), run_bytes=_v(v, "run_bytes"),
                            ntp_offset_s=float(_v(v, "ntp_offset_s"))))


def _b1(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    if not p.artifacts:
        return ["no pipeline record pinned for the installers (4.2)"]
    read = _v(v, "artifacts")
    return [f"{role} installer sha256 {read.get(role)} is not the pipeline record's {sha}"
            for role, sha in p.artifacts.items() if read.get(role) != sha]


def _b2(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    if p.install_branch == "A":
        return [] if _v(v, "auto_update_disabled") is True else ["the launcher's auto-update flag is not disabled"]
    build = _v(v, "snapshot_build_id")
    return [] if build in p.builds else [f"snapshot build {build!r} is not one of the pair's builds {list(p.builds)}"]


_B3_FIELDS = (("vmoptions_sha256", "vmoptions_hash"), ("jre_release_sha256", "jre_release_sha256"),
              ("java2d_pinned", "java2d_pipeline"))


def _b3(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    if "manifest" not in p.b3_references:
        return ["no snapshot manifest to compare vmoptions, JRE and Java2D with (choice 6)"]
    out: list[str] = []
    for name, ref in p.b3_references.items():
        for key, ref_key in _B3_FIELDS:
            if _v(v, key) != ref.get(ref_key):
                out.append(f"{key} {v[key]!r} differs from the {name}'s {ref.get(ref_key)!r}")
    return out


def _h1(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    _want(out, "tokened click received", _v(v, "click_received"), True)
    _want(out, "key chord received", _v(v, "chord_received"), True)
    return out


def _h2(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    for key in ("installed", "event_received", "removed"):
        _want(out, key, _v(v, key), True)
    lat = sorted(float(x) for x in _v(v, "latencies_ms"))
    if len(lat) < H2_MIN_EVENTS:
        out.append(f"{len(lat)} callback latencies, {H2_MIN_EVENTS} needed (10.8)")
    elif (p999 := lat[math.ceil(0.999 * len(lat)) - 1]) >= H2_P999_MAX_MS:
        out.append(f"callback latency p99.9 {p999:.2f} ms is not under {H2_P999_MAX_MS} ms")
    if lat and lat[-1] >= HOOK_TIMEOUT_MS:
        out.append(f"worst callback latency {lat[-1]:.1f} ms is not under the {HOOK_TIMEOUT_MS:.0f} ms hook timeout")
    recorders = {n.casefold() for n in p.recorder_images}
    found = sorted({str(n) for n in _v(v, "processes") if str(n).casefold() in recorders})
    if found:
        out.append(f"Recorder process present: {', '.join(found)} (5.16)")
    return out


def _h3(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    probe, want = np.asarray(_v(v, "probe")), probe_pattern()
    if probe.shape != want.shape or probe.dtype != want.dtype or not np.array_equal(probe, want):
        out.append("the probe window capture is not the known pattern byte for byte")
    if frame_uniform(_v(v, "desktop")):
        out.append("the desktop probe is uniform (locked or headless console)")
    if not _awareness_ok(_v(v, "awareness")):
        out.append(f"dpi_awareness: GetProcessDpiAwareness {v['awareness']!r}, expected {EXPECTED_AWARENESS}")
    return out


def _h4(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    drops = sorted(str(d) for d in _v(v, "drops"))
    want = sorted(h4_expected_drops())
    return [] if drops == want else [f"logged drops {drops}, expected exactly {want}"]


def _h5(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    read = PreflightRead("H5", v)
    return [] if h5_passed(read) else [f"{k} did not round-trip" for k in ("file_flag", "named_pipe")
                                       if v.get(k) is not True]


def _h6(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    _want(out, "synthetic account id masked", _v(v, "masked"), True)
    _want(out, "egress log entry", _v(v, "egress_logged"), True)
    return out


def _h7(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    qc = v.get("quick_check")
    out = [] if qc == "ok" else [f"quick_check: {qc if qc is not None else 'not read'}"]
    for key in ("db_local", "artifacts_local", "wal", "lock", "writable", "config_commit_ok", "lint_hash_ok"):
        if v.get(key) is not True:
            out.append(f"{key} {'not checked' if v.get(key) is None else 'failed'}")
    floor = rig_floor_bytes(int(_v(v, "shard_bytes")))
    if int(_v(v, "free_bytes")) <= floor:
        out.append(f"free space {v['free_bytes']} bytes is not above the R7 rig floor {floor} bytes")
    return out


def _h8(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    _want(out, "Watchdog alive", _v(v, "watchdog_alive"), True)
    if _v(v, "watchdog_pid") == _v(v, "orchestrator_pid"):
        out.append("the Watchdog PID is the Orchestrator's")
    if float(_v(v, "watchdog_age_s")) >= WATCHDOG_JSON_MAX_AGE_S:
        out.append(f"watchdog.json is {v['watchdog_age_s']} s old, not under {WATCHDOG_JSON_MAX_AGE_S:.0f} s")
    _want(out, "heartbeat.json read back", _v(v, "heartbeat_read_back"), True)
    return out


def _h9(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    labels: Mapping[str, Any] = _v(v, "labels")
    out = [f"label {want!r} read as {got!r}" for want, got in labels.items() if got != want]
    if not labels:
        out.append("no known label images were read")
    if p.jab_branch == "A" and v.get("jab_answers") is not True:
        out.append("the Java Access Bridge did not answer (DR-01 Branch A)")
    return out


def _h10(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    text = str(_v(v, "vmoptions"))
    out: list[str] = []
    if p.vmoptions_sha256 is None:
        out.append("no rendered vmoptions template hash pinned")
    elif vmoptions_sha256(text) != p.vmoptions_sha256:
        out.append(f"vmoptions hash {vmoptions_sha256(text)} is not the template's {p.vmoptions_sha256}")
    flags = tuple(f.replace("-XX:+", "-XX:") for f in CRASH_FLAGS) + CRASH_FLAGS
    out += [x for x in validate_vmoptions(text, p.rig_kind, p.jdk_major) if any(f in x for f in flags)]
    _want(out, "ErrorFile folder writable", _v(v, "error_dir_writable"), True)
    _want(out, "HeapDumpPath folder writable", _v(v, "dump_dir_writable"), True)
    _want(out, "WER LocalDumps key present", _v(v, "wer_localdumps"), True)
    return out


def _n1(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    hosts: Mapping[str, Mapping[str, Any]] = _v(v, "hosts")
    if not p.n1_hosts:
        return ["no N1 hosts pinned"]
    out = []
    for h in p.n1_hosts:
        r = hosts.get(h) or {}
        if r.get("reachable") is not True:
            out.append(f"{h} unreachable")
        elif r.get("chain_trusted") is not True:
            out.append(f"{h}: the served chain does not end in the trusted proxy CA")
    return out


def _n2(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    if not p.model_ids:
        return ["no pinned model ids"]
    models: Mapping[str, Any] = _v(v, "models")
    out = [f"GET /v1/models/{m} failed" for m in p.model_ids if models.get(m) is not True]
    _want(out, "1-token message", _v(v, "message_ok"), True)
    _want(out, "one-step agent request", _v(v, "agent_ok"), True)
    if int(_v(v, "cache_read_tokens")) <= 0:
        out.append("zero cache read on the second identical request: a silent prompt change (10.8)")
    return out


def _n3(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    q = float(_v(v, "quote_s"))
    return [] if q < QUOTE_MAX_S else [f"fixture quote took {q:.1f} s, not under {QUOTE_MAX_S:.0f} s"]


def _n4(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    if p.data_fixture_branch == "B":
        return [] if _v(v, "ondemand_available") is True else ["OnDemand is not available on the login (DR-02 B)"]
    out: list[str] = []
    _want(out, "fixture health", _v(v, "healthy"), True)
    if p.dataset_id is None:
        out.append("no dataset id pinned (DR-02 A)")
    else:
        _want(out, "dataset id", _v(v, "dataset_id"), p.dataset_id)
    return out


def _n5(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    if int(_v(v, "bytes")) < EGRESS_PROBE_BYTES:
        out.append(f"the probe POST carried {v['bytes']} bytes, not 2 MB")
    _want(out, "2 MB redacted POST accepted", _v(v, "accepted"), True)
    return out


def _n6(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    _want(out, "vault reachable", _v(v, "reachable"), True)
    unresolved = list(_v(v, "unresolved"))
    if unresolved:
        out.append(f"secret references not resolved: {', '.join(map(str, unresolved))}")
    return out


def _n7(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    return [f"{h} unreachable" for h in _v(v, "unreachable")]


def _g1(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out: list[str] = []
    if int(_v(v, "lint_errors")) != 0:
        out.append(f"sentinel lint reported {v['lint_errors']} errors")
    for key in ("commit_ok", "lint_hash_ok", "ruling_ok", "templates_ok"):
        _want(out, key, _v(v, key), True)
    return out


def _g2(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    if not p.corpus:
        return ["no corpus entries pinned"]
    files: Mapping[str, Any] = _v(v, "files")
    out = []
    for cid, sha in sorted(p.corpus.items()):
        got = files.get(cid)
        if got is None:
            out.append(f"{cid}: file missing from usergui")
        elif got != sha:
            out.append(f"{cid}: hash {got} is not the corpus entry's {sha}")
    return out


def _g3(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    cats: Mapping[str, Any] = _v(v, "catalogs")
    out = []
    for path in CATALOGS:
        got = cats.get(path)
        if got is None:
            out.append(f"{path} missing or unreadable")
        elif path in p.catalogs and got != p.catalogs[path]:
            out.append(f"{path} hash {got} is not the config commit's {p.catalogs[path]}")
    return out


def _g4(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    out = [f"no exchange session resolved for {s} through the 90-day horizon" for s in _v(v, "unresolved")]
    if not v.get("list_24h_sha256"):
        out.append("the 24-hour trading list was not read for the epoch (7.10)")
    return out


def _g5(v: Mapping[str, Any], p: PreflightPins) -> list[str]:
    result = str(_v(v, "result"))
    return [] if result == "pass" else [result]


EVALUATORS: dict[str, Evaluator] = {
    "R1": _r1, "R2": _r2, "R3": _r3, "R4": _r4, "R5": _r5, "R6": _r6, "R7": _r7, "B1": _b1, "B2": _b2, "B3": _b3,
    "H1": _h1, "H2": _h2, "H3": _h3, "H4": _h4, "H5": _h5, "H6": _h6, "H7": _h7, "H8": _h8, "H9": _h9, "H10": _h10,
    "N1": _n1, "N2": _n2, "N3": _n3, "N4": _n4, "N5": _n5, "N6": _n6, "N7": _n7,
    "G1": _g1, "G2": _g2, "G3": _g3, "G4": _g4, "G5": _g5,
}


def evaluate(item: str, read: PreflightRead, pins: PreflightPins) -> ItemResult:
    """One 12.7 item from its raw read (module docstring); an unreadable read or a missing value fails the item."""
    if item not in EVALUATORS:
        raise ValueError(f"{item!r} is not a 12.7 preflight item")
    if read.item != item:
        raise ValueError(f"a read of {read.item} evaluated as {item}")
    v = read.values
    control = v.get("blocking_control") or None
    shot = v.get("screenshot_sha256") or None
    if not read.readable:
        return ItemResult(item, False, (f"unreadable: {read.detail or 'no reason given'}",), control, shot)
    try:
        causes = EVALUATORS[item](v, pins)
    except _Missing as e:
        causes = [f"{e.args[0]} not read"]
    if control and causes:
        causes.append(f"blocked by {control}")
    return ItemResult(item, not causes, tuple(causes), control, shot)


# -- reads from the harness side --------------------------------------------------------------------------------
def merge_reads(*reads: PreflightRead) -> PreflightRead:
    """One read of an item from several sources (the rig's half and the harness's half of R7 or H7); a value read
    twice must agree."""
    if not reads or len({r.item for r in reads}) != 1:
        raise ValueError("merge reads of one item")
    values: dict[str, Any] = {}
    for r in reads:
        for k, x in r.values.items():
            if k in values and values[k] is not x and values[k] != x:
                raise ValueError(f"{r.item}: {k} read as {values[k]!r} and {x!r}")
            values[k] = x
    details = [r.detail for r in reads if r.detail]
    return PreflightRead(reads[0].item, values, all(r.readable for r in reads), "; ".join(details) or None)


def combine(rig: Callable[[str], PreflightRead],
            local: Mapping[str, Callable[[], PreflightRead]]) -> Callable[[str], PreflightRead]:
    """The read function `run_preflight` takes: the harness's own reads for the items in `local` (G1, G3, G4, G5), the
    rig read merged with the harness half for R7 and H7 (`MERGED_ITEMS`), the rig read for everything else."""
    stray = sorted(set(local) - set(CONTROLLER_ITEMS) - set(MERGED_ITEMS))
    if stray:
        raise ValueError(f"{stray} are read on the rig; the harness reads only {CONTROLLER_ITEMS + MERGED_ITEMS}")

    def read(item: str) -> PreflightRead:
        if item not in local:
            return rig(item)
        mine = local[item]()
        return merge_reads(rig(item), mine) if item in MERGED_ITEMS else mine
    return read


def g1_read(*, lint_errors: int, commit_ok: bool, lint_hash_ok: bool, ruling_ok: bool,
            templates_ok: bool) -> PreflightRead:
    """G1 (4.15, 6.11): `sentinel lint --all --config-commit <sha>` errors, the checkout at the pinned commit with the
    CI lint hash, the computer-use ruling read and valid, the landmark templates present."""
    return PreflightRead("G1", {"lint_errors": lint_errors, "commit_ok": commit_ok, "lint_hash_ok": lint_hash_ok,
                                "ruling_ok": ruling_ok, "templates_ok": templates_ok})


def catalog_versions(config_root: str | Path) -> dict[str, str | None]:
    """G3: the version of each catalog, the sha256 hex of its bytes (as `canary.Catalog.version`), None when missing;
    the non-None entries are `RunPlan.catalog_versions`, written to `runs.catalog_versions_json`."""
    root = Path(config_root)
    out: dict[str, str | None] = {}
    for rel in CATALOGS:
        try:
            out[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        except OSError:
            out[rel] = None
    return out


def g3_read(config_root: str | Path) -> PreflightRead:
    """G3 (12.7, 10.3): the popup, mutant and quarantine catalog versions of the pinned config checkout."""
    return PreflightRead("G3", {"catalogs": catalog_versions(config_root)})


def g4_read(symbols: Iterable[str], run_date: date, resolves: Callable[[str, date, date], bool], *,
            list_24h_sha256: str | None) -> PreflightRead:
    """G4 (12.7, 7.10): exchange sessions resolved for every fixture symbol over the run date plus 90 days, through
    the calendar's `resolves(symbol, start, end)`, and the 24-hour list hash read for the epoch."""
    end = run_date + timedelta(days=G4_HORIZON_DAYS)
    unresolved = [s for s in symbols if not resolves(s, run_date, end)]
    return PreflightRead("G4", {"unresolved": unresolved, "horizon_end": end.isoformat(),
                                "list_24h_sha256": list_24h_sha256})


def g5_read(result: str) -> PreflightRead:
    """G5 (12.7, SD-78): `fingerprint.g5`'s result, `pass`, `fingerprint_mismatch:<fields>` or `no_calibration`."""
    return PreflightRead("G5", {"result": result})


class SimulatedPreflightReads:
    """A `ports.PreflightReads` for the simulated rig: every item reads the values that pass against `pins` on a 7.1 VM
    rig, except `overrides`, one mapping of values per attempt of an item, consumed in order (a scripted failure, or a
    failure followed by a pass). A pin left unset gives the read nothing to match, so that item fails as on a real rig.
    `vmoptions` is the written file H10 reads (its sha256 is `pins.vmoptions_sha256` on a healthy rig)."""

    def __init__(self, pins: PreflightPins, *, vmoptions: str | None = None,
                 overrides: Mapping[str, Sequence[Mapping[str, Any]]] | None = None):
        self.pins = pins
        self.vmoptions = vmoptions
        self.overrides = {k: list(v) for k, v in (overrides or {}).items()}

    def _good(self, item: str) -> dict[str, Any]:
        p = self.pins
        wb = p.windows_build or {}
        width, height, scale = p.display.get("width"), p.display.get("height"), p.display.get("scale_percent")
        desktop = np.arange(4 * 4 * 3, dtype=np.uint8).reshape(4, 4, 3)
        manifest = p.b3_references.get("manifest", {})
        floor_free = 1000 * 1024 ** 3
        good: dict[str, dict[str, Any]] = {
            "R1": {"manifest_sha256": p.snapshot_manifest_sha256, "current_build": wb.get("current_build"),
                   "ubr": wb.get("ubr"), "pending_restart": False},
            "R2": {"width": width, "height": height, "cx": width, "cy": height,
                   "bits": RIG_SPEC_PINS["display.bits"], "frequency": RIG_SPEC_PINS["display.frequency"],
                   "dpi": round(96 * scale / 100) if isinstance(scale, int) else None,
                   "monitors": RIG_SPEC_PINS["display.monitors"], "awareness": EXPECTED_AWARENESS},
            "R3": {"smoothing": RIG_SPEC_PINS["cleartype.smoothing"], "type": RIG_SPEC_PINS["cleartype.type"],
                   **{k: (p.cleartype or {}).get(k) for k in ("gamma", "contrast", "orientation")}},
            "R4": {"time_zone": RIG_SPEC_PINS["time_zone.os"], "layout": RIG_SPEC_PINS["keyboard.layout"],
                   "layout_count": RIG_SPEC_PINS["keyboard.layout_count"],
                   **{k: RIG_SPEC_PINS[f"keyboard.toggle_keys.{k}"] for k in ("caps", "num", "scroll")},
                   "locale": RIG_SPEC_PINS["keyboard.locale"], **R4_FORMATS},
            "R5": dict(R5_PINS),
            "R6": {"input_desktop": RIG_SPEC_PINS["input_desktop"], "console": True, "active": True, "rdp": False,
                   "locked": False, "foreground_hwnd": 1, "probe": desktop},
            "R7": {"rig_free_bytes": floor_free, "shard_bytes": 0, "control_free_bytes": floor_free, "run_bytes": 0,
                   "ntp_offset_s": 0.0},
            "B1": {"artifacts": dict(p.artifacts)},
            "B2": ({"auto_update_disabled": True} if p.install_branch == "A"
                   else {"snapshot_build_id": p.builds[0] if p.builds else None}),
            "B3": {k: manifest.get(ref) for k, ref in _B3_FIELDS},
            "H1": {"click_received": True, "chord_received": True},
            "H2": {"installed": True, "event_received": True, "removed": True,
                   "latencies_ms": [0.1] * H2_MIN_EVENTS, "processes": []},
            "H3": {"probe": probe_pattern(), "desktop": desktop, "awareness": EXPECTED_AWARENESS},
            "H4": {"drops": list(h4_expected_drops())},
            "H5": {"file_flag": True, "named_pipe": True},
            "H6": {"masked": True, "egress_logged": True},
            "H7": {"db_local": True, "artifacts_local": True, "quick_check": "ok", "wal": True, "lock": True,
                   "writable": True, "free_bytes": floor_free, "shard_bytes": 0, "config_commit_ok": True,
                   "lint_hash_ok": True},
            "H8": {"watchdog_pid": 2, "orchestrator_pid": 1, "watchdog_alive": True, "watchdog_age_s": 1.0,
                   "heartbeat_read_back": True},
            "H9": {"labels": {"Last": "Last"}, "jab_answers": True},
            "H10": {"vmoptions": self.vmoptions, "error_dir_writable": True, "dump_dir_writable": True,
                    "wer_localdumps": True},
            "N1": {"hosts": {h: {"reachable": True, "chain_trusted": True} for h in p.n1_hosts}},
            "N2": {"models": {m: True for m in p.model_ids}, "message_ok": True, "agent_ok": True,
                   "cache_read_tokens": 1},
            "N3": {"quote_s": 0.5},
            "N4": ({"ondemand_available": True} if p.data_fixture_branch == "B"
                   else {"healthy": True, "dataset_id": p.dataset_id}),
            "N5": {"accepted": True, "bytes": EGRESS_PROBE_BYTES},
            "N6": {"reachable": True, "unresolved": []},
            "N7": {"unreachable": []},
            "G1": {"lint_errors": 0, "commit_ok": True, "lint_hash_ok": True, "ruling_ok": True, "templates_ok": True},
            "G2": {"files": dict(p.corpus)},
            "G3": {"catalogs": {c: p.catalogs.get(c, "0" * 64) for c in CATALOGS}},
            "G4": {"unresolved": [], "list_24h_sha256": "sha256:" + "0" * 64},
            "G5": {"result": "pass"},
        }
        return good[item]

    def read(self, item: str) -> PreflightRead:
        """The item's read: the next scripted override merged over the passing values, else the passing values."""
        if item not in EVALUATORS:
            raise ValueError(f"{item!r} is not a 12.7 preflight item")
        values = self._good(item)
        queue = self.overrides.get(item)
        if queue:
            values.update(queue.pop(0))
        return PreflightRead(item, values)


# -- the run ----------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PreflightRun:
    """Both attempts of every item, the final verdicts and the report `gates.check_provisioned` takes."""

    report: PreflightReport
    attempts: tuple[PreflightAttempt, ...]
    results: Mapping[str, ItemResult]
    dpi_awareness: str | None = None

    def run_health(self) -> list[dict[str, Any]]:
        """12.7 "Run health lists every item with pass or fail": one row per item, with every attempt."""
        rows = []
        for item, res in self.results.items():
            rows.append({"item": item, "passed": res.passed, "control": res.control,
                         "screenshot_sha256": res.screenshot_sha256,
                         "attempts": [{"attempt": a.attempt, "passed": a.passed, "at": a.at, "detail": a.detail}
                                      for a in self.attempts if a.item == item]})
        return rows


def _report_detail(res: ItemResult) -> str | None:
    """choice 2: what `report.detail[item]` holds for check_provisioned."""
    if res.passed:
        return None
    if res.item.startswith("H"):
        return res.control
    return res.detail


def run_preflight(reads_fn: Callable[[str], PreflightRead], clock: Clock, pins: PreflightPins, *, run_id: str,
                  shard_id: str | None = None, items: Sequence[str] = PREFLIGHT_ITEMS) -> PreflightRun:
    """Every item, a 60 s wait, the failed items once more (choice 1); both attempts logged as PreflightAttempt."""
    attempts: list[PreflightAttempt] = []
    results: dict[str, ItemResult] = {}
    awareness: str | None = None

    def attempt(item: str, n: int) -> None:
        nonlocal awareness
        read = reads_fn(item)
        res = evaluate(item, read, pins)
        if item == "H3" and isinstance(read.values.get("awareness"), str):
            awareness = read.values["awareness"]
        attempts.append(PreflightAttempt(run_id=run_id, item=item, attempt=n, passed=res.passed,
                                         at=clock.now().isoformat(), shard_id=shard_id, detail=res.detail))
        results[item] = res

    for item in items:
        attempt(item, 1)
    failed = [i for i in items if not results[i].passed]
    if failed:
        clock.sleep(RETRY_WAIT_S)
        for item in failed:
            attempt(item, 2)
    detail = {i: d for i in items if (d := _report_detail(results[i]))}
    report = PreflightReport({i: results[i].passed for i in items}, detail)
    return PreflightRun(report, tuple(attempts), {i: results[i] for i in items}, awareness)

