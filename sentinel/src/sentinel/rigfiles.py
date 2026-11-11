"""The rig's installed files and run-start reads, judged without Windows (7.1, 7.3, 7.12; 12.7 B3 and H10).

- `parse_jre_release(data)`: the bundled runtime's `release` file (7.3 version read step 3): JAVA_VERSION, IMPLEMENTOR
  and OS_ARCH, the whole file hashed; `JreRelease.fingerprint()` is the 4.12 `jre_release` field and `major` the JDK
  major the vmoptions flags are checked against.
- `jar_listing_hash(entries)`: the sorted names and sizes under the install folder, hashed (7.3 step 4); the 4.12
  record field `jar_listing_hash`.
- `parse_vmoptions(text)`, `validate_vmoptions(text, rig_kind, jdk_major)`, `pinned_java2d(text)` and
  `vmoptions_sha256(text)`: `thinkorswim.vmoptions` and its checked-in template (7.1 Java2D and Memory rows, 7.3 step 4,
  7.12 Crash capture, 10.2 JVM flags, L-XREF-28). The validator is the one the lint (`lint/checks/rigfiles.py`), the
  Rig Manager (`rigmanager.verify_vmoptions`) and preflight B3 and H10 (`orchestrator.preflight`) use.
- `load_process_allowlist(path)` and `check_processes(processes, allowlist)`: the running-process allowlist of 7.1
  ("Other software") and 7.12 ("Harness processes").
- `check_hardening(reads)`: the 7.12 run-start reads with no 12.7 item: audio muted, SPI_GETMOUSE pointer precision
  and speed, the visual effects outside the fingerprint, and the foreign-window count.

Choices where the spec is ambiguous or silent (reported):

1. Hashes use the 4.13 `sha256:` notation over the exact bytes: the release file as read, the vmoptions text as
   UTF-8, and the jar listing as `<name>\\t<size>` lines sorted by name, with `/` as the separator and a final newline.
2. JVM flags (L-XREF-28, 7.14 check 3): `JVM_FLAGS` lists each `-X` and `-XX` flag a template may carry with the first
   JDK major that accepts it and the first that refuses it. A flag outside the table is refused, since no one has
   checked it against a JDK major; `-D` system properties are always accepted because the JVM takes any property.
   `-XX:+CreateCoredumpOnCrash` needs JDK 9 or later (JDK 8 named it CreateMinidumpOnCrash). Without a JDK major
   (the lint: `phase0/branches.yaml` records none yet) a flag only has to be in the table. HotSpot refuses `+`/`-`
   on a value flag and a value on a boolean flag, and so does the validator (`XX_BOOLEAN` names the boolean ones).
3. The Java2D pipeline a vmoptions text pins (4.12 `java2d_pipeline.pinned`), read as the JVM reads it
   (`WindowsFlags`): each `sun.java2d.*` boolean is true when its value is exactly '', `t`, `T`, `true` or `True`
   (a bare `-Dsun.java2d.opengl` included), false when exactly `f`, `F`, `false` or `False`, and any other
   spelling (`TRUE`, `FALSE`) leaves the default. `opengl` when `sun.java2d.opengl` reads true (the static reading
   fails closed: the JVM falls back only where WGL is missing); else Direct3D is on when `sun.java2d.d3d` reads
   true, defaulting to "not `sun.java2d.noddraw`" (noddraw disables Direct3D unless d3d is set), `d3d`, and
   otherwise `software`. A VM rig needs exactly `-Dsun.java2d.d3d=false` and `-Dsun.java2d.noddraw=true`; the
   physical rig needs exactly `-Dsun.java2d.d3d=true` (7.1, L-XREF-28); any other value of a `sun.java2d.*`
   pipeline property that the JVM would ignore is refused, since it reads as a pin it is not.
4. Process allowlist (7.1, 7.12, SD-71): image names compare case-insensitively, as Windows does. Each harness image
   (Orchestrator, Gateway hook host, Watchdog and the elevated helper service of `harness`) may run once: a second
   instance is a fourth harness process in the session and is refused. A missing harness process is not this check's
   (H8 proves the Watchdog, H1 and H2 the hook host, and the Orchestrator is the reader). Any image in none of the
   three lists is refused. The outcome is `env_error (gate: provisioned, reason: rig_spec_mismatch)` naming every
   offending image.
5. Hardening reads (7.12): audio muted, Enhance pointer precision off, pointer speed 10 (the Windows default),
   taskbar animations off, a solid-color wallpaper, the taskbar never auto-hidden and no foreign top-level window.
   Window animations and transparency are fingerprint fields (`os_theme`) that `fingerprint.rig_spec_check` already
   judges, so they are not read twice. No 12.7 item reads these values, so SD-71 makes a difference
   `env_error (gate: provisioned, reason: rig_spec_mismatch)`; an unreadable value (None) takes the same outcome
   with "unreadable" in `detail`, since the pinned setting cannot be confirmed.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .canonical import SHA256_PREFIX
from .lint.rules import RIG_KIND_PIPELINE
from .orchestrator import alerts
from .orchestrator.ports import ProcessInfo
from .orchestrator.results import ControlResult, Outcome, RunVerdict

# -- the JRE release file (7.3 version read 3) ------------------------------------------------------------------
_RELEASE_LINE = re.compile(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"?(.*?)"?\s*$')


def sha256_notation(data: bytes) -> str:
    """`sha256:` plus the hex digest of `data` (4.13 notation)."""
    return SHA256_PREFIX + hashlib.sha256(data).hexdigest()


def java_major(version: str) -> int:
    """The JDK major of a JAVA_VERSION string: `1.8.0_292` is 8, `11.0.12` is 11, `17` is 17."""
    m = re.match(r"^\s*(\d+)(?:\.(\d+))?", version)
    if m is None:
        raise ValueError(f"JAVA_VERSION {version!r} has no major version")
    first = int(m.group(1))
    return int(m.group(2) or 0) if first == 1 else first


@dataclass(frozen=True)
class JreRelease:
    """The bundled runtime's `release` file (7.3): the whole file hashed and the three values the fingerprint uses."""

    sha256: str
    java_version: str
    implementor: str | None = None
    os_arch: str | None = None

    @property
    def major(self) -> int:
        """The JDK major the vmoptions flags are checked against (7.3 step 4)."""
        return java_major(self.java_version)

    def fingerprint(self) -> dict[str, str]:
        """The 4.12 `match.jre_release` field."""
        return {"sha256": self.sha256, "java_version": self.java_version}


def parse_jre_release(data: bytes | str) -> JreRelease:
    """Parse the `release` file of the bundled runtime (7.3 version read 3). ValueError when JAVA_VERSION is absent:
    the field is then unreadable (`fingerprint_unreadable:jre_release`, 7.1)."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    values: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        m = _RELEASE_LINE.match(line)
        if m is not None:
            values.setdefault(m.group(1), m.group(2))
    version = values.get("JAVA_VERSION")
    if not version:
        raise ValueError("the release file has no JAVA_VERSION (7.3)")
    return JreRelease(sha256_notation(raw), version, values.get("IMPLEMENTOR"), values.get("OS_ARCH"))


# -- the jar listing (7.3 version read 4) -----------------------------------------------------------------------
def jar_listing_hash(entries: Iterable[tuple[str, int]]) -> str:
    """The hash of the sorted names and sizes under the install folder (7.3 step 4, choice 1); the order of `entries`
    does not matter. ValueError on a repeated name or a negative size."""
    rows: dict[str, int] = {}
    for name, size in entries:
        key = str(name).replace("\\", "/")
        if key in rows:
            raise ValueError(f"{key} listed twice")
        if int(size) < 0:
            raise ValueError(f"{key} has a negative size")
        rows[key] = int(size)
    text = "".join(f"{k}\t{rows[k]}\n" for k in sorted(rows))
    return sha256_notation(text.encode("utf-8"))


# -- thinkorswim.vmoptions (7.1, 7.3 step 4, 7.12, 10.2, L-XREF-28) ------------------------------------------------
HEAP_FLAGS = ("-Xms", "-Xmx")
CRASH_FLAGS = ("-XX:ErrorFile", "-XX:+HeapDumpOnOutOfMemoryError", "-XX:HeapDumpPath", "-XX:+CreateCoredumpOnCrash")
JAVA2D_FLAGS: dict[str, dict[str, str]] = {        # choice 3: 7.1 Java2D pipeline row
    "vm": {"sun.java2d.d3d": "false", "sun.java2d.noddraw": "true"},
    "physical": {"sun.java2d.d3d": "true"},
}
JAVA2D_PROPERTIES = ("sun.java2d.d3d", "sun.java2d.noddraw", "sun.java2d.opengl")     # choice 3: what pins the pipeline
JAVA2D_TRUE = ("", "t", "T", "true", "True")       # choice 3: WindowsFlags.getBooleanProp, exact strings
JAVA2D_FALSE = ("f", "F", "false", "False")
# choice 2: flag key -> (first JDK major that accepts it, first that refuses it or None)
JVM_FLAGS: dict[str, tuple[int, int | None]] = {
    "-Xms": (1, None), "-Xmx": (1, None), "-Xss": (1, None),
    "-XX:ErrorFile": (6, None), "-XX:HeapDumpOnOutOfMemoryError": (6, None), "-XX:HeapDumpPath": (6, None),
    "-XX:CreateCoredumpOnCrash": (9, None), "-XX:CreateMinidumpOnCrash": (1, 9),
    "-XX:MaxMetaspaceSize": (8, None), "-XX:UseG1GC": (7, None),
}
XX_BOOLEAN = frozenset({"-XX:HeapDumpOnOutOfMemoryError", "-XX:CreateCoredumpOnCrash", "-XX:CreateMinidumpOnCrash",
                        "-XX:UseG1GC"})             # choice 2: given as -XX:+Name / -XX:-Name; the rest take =value
_SIZE_FLAG = re.compile(r"^(-X(?:ms|mx|ss))(\S+)$")
_XX_BOOL = re.compile(r"^-XX:([+-])([A-Za-z][A-Za-z0-9]*)$")
_XX_VALUE = re.compile(r"^-XX:([A-Za-z][A-Za-z0-9]*)=(.*)$")
_PROPERTY = re.compile(r"^-D([^=\s]+)(?:=(.*))?$")


@dataclass(frozen=True)
class VmOption:
    """One line of a vmoptions file: its key (`-Xmx`, `-XX:HeapDumpPath`, `-Dsun.java2d.d3d`), its value (`+` or `-`
    for a boolean `-XX` flag) and the line it came from."""

    key: str
    value: str
    line: int
    raw: str

    @property
    def property_name(self) -> str | None:
        """The system property a `-D` option sets, else None."""
        return self.key[2:] if self.key.startswith("-D") else None


def parse_vmoptions(text: str) -> tuple[tuple[VmOption, ...], tuple[str, ...]]:
    """The options of a vmoptions text and the problems of lines that are not JVM options. Blank lines and `#` comment
    lines are skipped (install4j vmoptions syntax); every other line holds one option."""
    opts: list[VmOption] = []
    problems: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if m := _SIZE_FLAG.match(s):
            opts.append(VmOption(m.group(1), m.group(2), n, s))
        elif m := _XX_BOOL.match(s):
            opts.append(VmOption(f"-XX:{m.group(2)}", m.group(1), n, s))
        elif m := _XX_VALUE.match(s):
            opts.append(VmOption(f"-XX:{m.group(1)}", m.group(2), n, s))
        elif m := _PROPERTY.match(s):
            opts.append(VmOption(f"-D{m.group(1)}", m.group(2) or "", n, s))
        elif s.startswith("-") and " " not in s:
            opts.append(VmOption(s, "", n, s))
        else:
            problems.append(f"line {n}: {s!r} is not a JVM option")
    return tuple(opts), tuple(problems)


def _properties(opts: Iterable[VmOption]) -> dict[str, str]:
    return {o.property_name: o.value for o in opts if o.property_name is not None}


def _java2d_bool(value: str | None, default: bool) -> bool:
    """A `sun.java2d.*` boolean as the JVM reads it (choice 3); None (absent) and unread spellings give `default`."""
    if value in JAVA2D_TRUE:
        return True
    if value in JAVA2D_FALSE:
        return False
    return default


def pinned_java2d(text: str) -> str:
    """The Java2D pipeline a vmoptions text pins (choice 3): `opengl`, `software` or `d3d`."""
    props = _properties(parse_vmoptions(text)[0])
    if _java2d_bool(props.get("sun.java2d.opengl"), False):
        return "opengl"
    directdraw = not _java2d_bool(props.get("sun.java2d.noddraw"), False)
    return "d3d" if _java2d_bool(props.get("sun.java2d.d3d"), directdraw) else "software"


def vmoptions_sha256(text: str) -> str:
    """The sha256 of a vmoptions text as written (choice 1): the B3 and H10 hash and the 4.12 `vmoptions_hash`."""
    return sha256_notation(text.encode("utf-8"))


def _flag_known(key: str, jdk_major: int | None) -> str | None:
    """None when the flag is acceptable for the JDK major (choice 2), else the problem."""
    if key.startswith("-D"):
        return None
    span = JVM_FLAGS.get(key)
    if span is None:
        return f"{key} is not a flag checked against a JDK major (7.3 step 4)"
    since, until = span
    if jdk_major is not None and (jdk_major < since or (until is not None and jdk_major >= until)):
        upto = f" before JDK {until}" if until is not None else ""
        return f"{key} is not known to JDK {jdk_major} (accepted from JDK {since}{upto})"
    return None


def _xx_form_problem(o: VmOption, cite: str) -> list[str]:
    """A known `-XX` flag in the wrong form (choice 2): a boolean given a value, or a value flag given `+`/`-`."""
    name = o.key.removeprefix("-XX:")
    bool_form = _XX_BOOL.match(o.raw) is not None
    if o.key in XX_BOOLEAN and not bool_form:
        return [f"line {o.line}: {o.raw} must be -XX:+{name} or -XX:-{name}{cite}"]
    if o.key not in XX_BOOLEAN and bool_form:
        return [f"line {o.line}: {o.raw} must be {o.key}=<value>{cite}"]
    return []


def validate_vmoptions(text: str, rig_kind: str = "vm", jdk_major: int | None = None) -> tuple[str, ...]:
    """The problems of a vmoptions text or template (L-XREF-28, 7.3 step 4); () when it is valid.

    Required: `-Xms` and `-Xmx`, the four crash flags of 7.12 and 10.2, and the Java2D flags of the rig kind (7.1);
    no option given twice; every flag known to the JDK major and in the form HotSpot accepts (choice 2); Java2D
    values spelled as the JVM reads them (choice 3)."""
    if rig_kind not in JAVA2D_FLAGS:
        raise ValueError(f"rig kind {rig_kind!r} is not one of {sorted(JAVA2D_FLAGS)} (7.1)")
    opts, problems = parse_vmoptions(text)
    out = list(problems)
    by_key: dict[str, VmOption] = {}
    for o in opts:
        if o.key in by_key:
            out.append(f"line {o.line}: {o.key} is given twice (first on line {by_key[o.key].line})")
            continue
        by_key[o.key] = o
        unknown = _flag_known(o.key, jdk_major)
        if unknown:
            out.append(f"line {o.line}: {unknown}")
    required = {flag.replace("-XX:+", "-XX:") for flag in CRASH_FLAGS}
    for key, o in by_key.items():
        if key.startswith("-XX:") and key in JVM_FLAGS and key not in required:
            out += _xx_form_problem(o, "")
    for flag in HEAP_FLAGS + CRASH_FLAGS:
        boolean = flag.startswith("-XX:+")
        key = flag.replace("-XX:+", "-XX:")
        found = by_key.get(key)
        if found is None:
            out.append(f"{flag} is missing (7.3 step 4, 7.12)")
        elif boolean and found.value != "+":
            out.append(f"line {found.line}: {found.raw} must be {flag}")
        elif not boolean and key.startswith("-XX:") and _XX_BOOL.match(found.raw):
            out += _xx_form_problem(found, " (7.12)")
        elif not boolean and not found.value.strip():
            out.append(f"line {found.line}: {flag} has no value")
    props = _properties(opts)
    pins = JAVA2D_FLAGS[rig_kind]
    for name, want in pins.items():
        have = props.get(name)
        if have != want:
            out.append(f"-D{name}={want} is required on a {rig_kind} rig (7.1), found "
                       f"{'nothing' if have is None else repr(have)}")
    for name in JAVA2D_PROPERTIES:
        have = props.get(name)
        if name not in pins and have is not None and have not in JAVA2D_TRUE + JAVA2D_FALSE:
            out.append(f"-D{name}={have!r} is not a value the JVM reads (exactly true, t, T, True, false, f, F, False "
                       f"or none); it keeps the default (7.1)")
    pipeline = pinned_java2d(text)
    if pipeline != RIG_KIND_PIPELINE[rig_kind]:
        out.append(f"the flags pin the {pipeline} Java2D pipeline; a {rig_kind} rig pins "
                   f"{RIG_KIND_PIPELINE[rig_kind]} (7.1)")
    return tuple(out)


# -- the running-process allowlist (7.1 Other software, 7.12 Harness processes) -------------------------------------
@dataclass(frozen=True)
class ProcessAllowlist:
    """`rig/process_allowlist.yaml` (4.16): the harness images, the thinkorswim tree and the golden image's Windows
    baseline, and the file's sha256."""

    image_id: str
    harness: tuple[str, ...]
    app_tree: tuple[str, ...]
    windows_baseline: tuple[str, ...]
    sha256: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], sha256: str = "") -> ProcessAllowlist:
        """The allowlist of a parsed file; KeyError when a list is missing."""
        def names(key: str) -> tuple[str, ...]:
            return tuple(str(x) for x in data[key])
        return cls(str(data["image_id"]), names("harness"), names("app_tree"), names("windows_baseline"), sha256)


def load_process_allowlist(path: str | Path) -> ProcessAllowlist:
    """Read `rig/process_allowlist.yaml` (4.16 process-allowlist schema)."""
    raw = Path(path).read_bytes()
    return ProcessAllowlist.from_mapping(yaml.safe_load(raw) or {}, sha256_notation(raw))


def _name(p: str | ProcessInfo) -> str:
    return (p.name if isinstance(p, ProcessInfo) else str(p)).strip()


def _rig_spec_mismatch(control: str, detail: str, evidence: dict[str, Any]) -> ControlResult:
    """`env_error (gate: provisioned, reason: rig_spec_mismatch)` on every scoped checkpoint (7.1, SD-71)."""
    return ControlResult(control, False, "provisioned", run=RunVerdict("complete_with_env_errors"),
                         unreached=Outcome("env_error", "rig_spec_mismatch", "provisioned", detail),
                         alert=alerts.env_error(), detail=detail, evidence=evidence)


def check_processes(processes: Sequence[str | ProcessInfo], allowlist: ProcessAllowlist) -> ControlResult | None:
    """The running-process allowlist at gate `provisioned` (choice 4): None when every process is allowed, else
    `env_error (gate: provisioned, reason: rig_spec_mismatch)` naming the offending images."""
    harness = {n.casefold() for n in allowlist.harness}
    allowed = harness | {n.casefold() for n in (*allowlist.app_tree, *allowlist.windows_baseline)}
    counts: dict[str, int] = {}
    foreign: list[str] = []
    for p in processes:
        name = _name(p)
        key = name.casefold()
        if key not in allowed:
            if name not in foreign:
                foreign.append(name)
            continue
        if key in harness:
            counts[name] = counts.get(name, 0) + 1
    extra = sorted(n for n, c in counts.items() if c > 1)
    if not foreign and not extra:
        return None
    parts = []
    if foreign:
        parts.append("not on the process allowlist: " + ", ".join(sorted(foreign)))
    if extra:
        parts.append("harness image running more than once: " + ", ".join(f"{n} x{counts[n]}" for n in extra))
    return _rig_spec_mismatch("process_allowlist", "; ".join(parts) + " (7.1, 7.12)",
                              {"foreign": sorted(foreign), "duplicate_harness": extra,
                               "image_id": allowlist.image_id})


# -- run-start hardening reads (7.12) -------------------------------------------------------------------------------
@dataclass(frozen=True)
class HardeningReads:
    """The 7.12 reads made at run start that no 12.7 item covers (choice 5); None is a value that could not be read."""

    audio_muted: bool | None
    mouse_enhance_precision: bool | None       # SPI_GETMOUSE acceleration: Enhance pointer precision
    mouse_speed: int | None                    # SPI_GETMOUSESPEED, 1 to 20
    taskbar_animations: bool | None
    wallpaper_solid: bool | None
    taskbar_autohide: bool | None
    foreign_windows: int | None                # foreign top-level windows on the desktop (POP-19)


HARDENING_PINS: dict[str, Any] = {"audio_muted": True, "mouse_enhance_precision": False, "mouse_speed": 10,
                                  "taskbar_animations": False, "wallpaper_solid": True, "taskbar_autohide": False,
                                  "foreign_windows": 0}


def check_hardening(reads: HardeningReads) -> ControlResult | None:
    """The 7.12 run-start hardening reads (choice 5): None when every read equals its pin, else
    `env_error (gate: provisioned, reason: rig_spec_mismatch)` naming each differing or unreadable read."""
    bad: list[str] = []
    for name, want in HARDENING_PINS.items():
        have = getattr(reads, name)
        if have is None:
            bad.append(f"{name} unreadable")
        elif have != want:
            bad.append(f"{name} is {have!r}, pinned {want!r}")
    if not bad:
        return None
    return _rig_spec_mismatch("hardening", "hardening reads: " + "; ".join(bad) + " (7.12)", {"reads": bad})
