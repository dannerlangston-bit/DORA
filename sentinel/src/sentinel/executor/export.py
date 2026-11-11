"""Export and Copy All steps (4.7 `export`, 5.4.10, 5.10, 2.8.1; SD-32): a grid's numbers as an Oracle source.

`run_export(step, clipboard, fs, *, drive, flag, clock, sleep, menu, export_dir)` runs one `export` step (`kind:
file` or `kind: copy_all`; a `copy_grid` step is `copy_all`). Every input goes through `drive`, the executor's run
of one primitive sub-step down the locator ladder and through the gateway (5.4.1: no other input path):

- `file`: the ladder-located menu clicks (`menu`, the grid's Actions menu and its export item from the Surface
  Registry), then the absolute path typed into the Swing file chooser with a guarded Enter as its commit, which the
  step requests explicitly and only while `flag()` reads clear (5.10); then the file is polled through `fs` until
  its text arrives or `timeout_ms` passes;
- `copy_all`: the clipboard cleared, a click on the grid, Ctrl+C (only while the flag is clear: a Ctrl chord is
  blocked under the flag, 5.4.7), the clipboard polled until text arrives or `timeout_ms` passes, and the clipboard
  cleared again whatever happened (5.10: "clipboard read after copy_grid, then cleared").

The `ExportResult` carries the step outcome and its SD-32 reason (`export_timeout` when nothing arrived in time,
`export_unavailable` when an export action failed at run time) and the table; `ExportResult.source(field_map)` is
the `oracle.ExportSource` the checkpoints that read the export get. The Oracle owns what that means for their
assertions: an export that produced nothing makes them `needs_human (reason: oracle_unreadable)`, a file whose
header set differs from `headers` makes them `harness_error (reason: export_header_mismatch)`, and a Copy All text
with another header falls back to OCR (oracle/sources.py, oracle/assertions.py; 2.8.1).

Choices where the spec is ambiguous or silent (reported):

1. The menu path of a file export is the caller's `menu`, target ids resolved from the Surface Registry for the
   grid (5.10 says "the ladder-located Actions menu" without naming its controls); Copy All focuses the grid with a
   click on its `target` and copies with Ctrl+C (5.4.10 "or Ctrl+C"), until Phase 0 names a menu path for it.
2. Without `path` the file goes to `<export_dir>\\<step id>.csv`; the path must be absolute (a Windows path, 5.10
   "a typed absolute path") and is typed as a flow literal, so it may hold no space (primitives choice 1).
3. With the flag set or unknown the guarded Enter (or the Ctrl+C) is not requested: the step is `failed` with
   `export_unavailable`, since requesting it would be an attempted send (5.4.3 stage 4); the open file chooser is
   left to the flow's teardown and the popup sentinel.
4. A file is taken once two polls 250 ms apart read the same non-empty text, so a file still being written is not
   read half way; at the timeout a non-empty text read once is taken rather than called a timeout.
5. The clipboard is also cleared before Ctrl+C, so text left there by an earlier copy is never read as this one.
6. A header mismatch is not a step failure: the export arrived, so the step passes and the Oracle gives the
   dependent checkpoints their outcome. A sub-step the gateway blocked makes the step `blocked_by_gateway` (no
   reason, 4.13); any other failed sub-step makes it `failed` with `export_unavailable`.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import Any, Protocol

from ..oracle.sources import ExportSource, ExportTable
from ..orchestrator.gateway import Flag
from ..orchestrator.ports import ClipboardPort
from ..taxonomy import load

POLL_S = 0.25
DEFAULT_TIMEOUT_MS = 5000          # 4.7 step common `timeout_ms`
PASSED = ("passed", "passed_after_retry", "passed_via_agent_fallback")
KINDS = ("file", "copy_all")


class ExportFiles(Protocol):
    """Where a file export lands, read on the rig (the export folder of the run)."""

    def read_text(self, path: str) -> str | None:
        """The file's text; None while it does not exist."""


StepDriver = Callable[[Mapping[str, Any]], str]
"""The executor's run of one primitive sub-step (4.7 step dict) down the ladder and through the gateway: its step
outcome."""


@dataclass(frozen=True)
class ExportResult:
    """What one export step did (module docstring)."""

    status: str
    kind: str
    headers: tuple[str, ...]
    reason: str | None = None                   # export_timeout | export_unavailable (SD-32)
    table: ExportTable | None = None
    path: str | None = None
    substeps: tuple[tuple[str, str], ...] = ()  # (sub-step id, outcome) in order
    detail: str | None = None
    text: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        load().check_step(self.status)
        if self.reason not in (None, "export_timeout", "export_unavailable"):
            raise ValueError(f"an export step's reason is export_timeout or export_unavailable, not {self.reason!r}")

    @property
    def produced(self) -> bool:
        return self.table is not None

    @property
    def header_matches(self) -> bool:
        return self.source().header_matches

    def source(self, field_map: Mapping[str, tuple[str, int]] | None = None) -> ExportSource:
        """The Oracle's export source for the checkpoints that read this export; `field_map` maps a region id to the
        (column, row) cell holding the same value."""
        return ExportSource(self.table, self.headers, kind=self.kind, field_map=field_map)

    def step_outcome_fields(self) -> dict[str, Any]:
        """The `records.StepOutcome` fields the export decides (4.13, SD-32)."""
        return {"status": self.status, "reason": self.reason}


def run_export(step: Mapping[str, Any], clipboard: ClipboardPort, fs: ExportFiles, *, drive: StepDriver,
               flag: Callable[[], Flag], clock: Callable[[], float], sleep: Callable[[float], None],
               menu: Sequence[str] = (), export_dir: str | None = None) -> ExportResult:
    """Run one export step (module docstring)."""
    kind = "copy_all" if step.get("type") == "copy_grid" else str(step.get("kind", ""))
    if step.get("type") not in ("export", "copy_grid") or kind not in KINDS:
        raise ValueError("run_export runs export steps of kind file or copy_all (4.7)")
    headers = step.get("headers")
    if not isinstance(headers, (list, tuple)) or not headers or not all(isinstance(h, str) for h in headers):
        raise ValueError("an export step declares the headers it must carry (4.7, SD-32)")
    sid, target = str(step.get("id", "export")), str(step.get("target", ""))
    timeout_s = int(step.get("timeout_ms", DEFAULT_TIMEOUT_MS)) / 1000.0
    run = _Run(tuple(headers), kind, drive)
    if kind == "file":
        path = _path(step, sid, export_dir)
        if not menu:
            raise ValueError("a file export needs the ladder targets of the grid's export menu (choice 1)")
        for i, item in enumerate(menu, 1):
            if (stop := run.sub({"id": f"{sid}-menu-{i}", "type": "click_named", "target": item})) is not None:
                return stop
        if flag() != Flag.CLEAR:
            return run.end("failed", "export_unavailable", path=path, detail="the file chooser's Enter is requested "
                           "only with the flag clear (5.10); not requested")
        if (stop := run.sub({"id": f"{sid}-path", "type": "type_fixture", "value": path, "commit": "enter"})) is not None:
            return stop
        text = _poll(lambda: fs.read_text(path), clock, sleep, timeout_s, stable=True)
        if text is None:
            return run.end("failed", "export_timeout", path=path, detail=f"no file at {path} within {timeout_s:g} s")
        return run.end("passed", None, path=path, table=ExportTable.parse(text, kind), text=text)
    clipboard.clear()
    try:
        if (stop := run.sub({"id": f"{sid}-focus", "type": "click_named", "target": target})) is not None:
            return stop
        if flag() != Flag.CLEAR:
            return run.end("failed", "export_unavailable", detail="Ctrl+C is requested only with the flag clear "
                           "(5.4.7); not requested")
        if (stop := run.sub({"id": f"{sid}-copy", "type": "key_chord", "keys": ["ctrl", "c"]})) is not None:
            return stop
        text = _poll(clipboard.read_text, clock, sleep, timeout_s, stable=False)
    finally:
        clipboard.clear()
    if text is None:
        return run.end("failed", "export_timeout", detail=f"no clipboard text within {timeout_s:g} s")
    return run.end("passed", None, table=ExportTable.parse(text, kind), text=text)


@dataclass
class _Run:
    headers: tuple[str, ...]
    kind: str
    drive: StepDriver
    done: list[tuple[str, str]] = field(default_factory=list)

    def sub(self, sub: Mapping[str, Any]) -> ExportResult | None:
        """Drive one sub-step; the step's result when it did not pass (choice 6), else None."""
        status = self.drive(sub)
        self.done.append((str(sub["id"]), status))
        if status in PASSED:
            return None
        if status == "blocked_by_gateway":
            return self.end("blocked_by_gateway", None, detail=f"sub-step {sub['id']} blocked by the gateway")
        return self.end("failed", "export_unavailable", detail=f"sub-step {sub['id']} {status}")

    def end(self, status: str, reason: str | None, *, path: str | None = None, table: ExportTable | None = None,
            text: str | None = None, detail: str | None = None) -> ExportResult:
        return ExportResult(status, self.kind, self.headers, reason, table, path, tuple(self.done), detail, text)


def _path(step: Mapping[str, Any], sid: str, export_dir: str | None) -> str:
    raw = step.get("path")
    if raw is None:
        if export_dir is None:
            raise ValueError("a file export without a path needs the run's export folder (choice 2)")
        raw = str(PureWindowsPath(export_dir) / f"{sid}.csv")
    path = PureWindowsPath(str(raw))
    if not path.is_absolute():
        raise ValueError(f"the export path {raw!r} is not absolute (5.10)")
    return str(path)


def _poll(read: Callable[[], str | None], clock: Callable[[], float], sleep: Callable[[float], None],
          timeout_s: float, *, stable: bool) -> str | None:
    """Read until non-empty text arrives (twice the same when `stable`, choice 4) or the timeout passes."""
    start, last = clock(), None
    while True:
        text = read()
        if text:
            if not stable or text == last:
                return text
            last = text
        if clock() - start >= timeout_s:
            return last
        sleep(POLL_S)
