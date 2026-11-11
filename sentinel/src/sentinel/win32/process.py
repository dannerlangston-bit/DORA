"""`ports.ProcessPort` over Win32 (5.14, 6.6 step 3, 10.2, 10.11).

- `process_tree(root)`: from a Toolhelp process snapshot (`CreateToolhelp32Snapshot`, `Process32FirstW/NextW`), the
  tree of 10.2 item 3: every process descended from the root or running from the pinned install folder (`app_tree`,
  pure, over `descendants`), each with the soak sampler's metrics: working set and private bytes
  (`GetProcessMemoryInfo`), GDI and USER objects (`GetGuiResources`), handles (`GetProcessHandleCount`), threads (the
  snapshot), CPU seconds (`GetProcessTimes`) and the image path (`QueryFullProcessImageNameW`).
- `kill_tree(root)`: the same tree, `TerminateProcess` children first (`kill_order`, pure), each waited on, then a
  fresh snapshot: True when no killed process and nothing whose image lies under the install folder survives (6.6
  step 3, 10.2 item 4).
- `crash_files(since)`: new `hs_err_pid*.log`, heap dumps and WER dumps in the crash folders since `since` (10.2);
  a plain directory scan (`find_crash_files`), so it runs and is tested anywhere.

Choices (reported):

1. A process counts as a child of its parent only when it was created after the parent (`GetProcessTimes`), so a
   process whose parent pid was reused by a later process is not pulled into the tree; a process whose creation time
   cannot be read is kept (the tree errs toward killing more of the app, never toward leaving part of it).
2. A metric that cannot be read (access denied, the process exited between the snapshot and the read) is None; the
   process stays in the tree. The metrics are read with PROCESS_QUERY_INFORMATION and PROCESS_VM_READ.
3. Kill: a process that cannot be opened for termination is skipped (gone already, or not ours); the survivors check
   decides. Each kill waits up to 5 s (`KILL_WAIT_MS`). A survivor is a process of the new snapshot with the pid and
   creation time of a killed one, or one whose image path is under `install_dir` (case-insensitive, as NTFS is).
4. Crash files: the folders are the caller's (`crash_dirs`: the install folder, where the JVM writes `hs_err_pid`
   files and heap dumps by default, the ErrorFile and HeapDumpPath folders of the vmoptions and the WER LocalDumps
   folder, 10.2, H10), scanned one level deep; a file is new when its modification time is at or after `since`, an
   ISO 8601 time with its UTC offset (a naive time is refused, so a local clock is never guessed).
5. The tree's two rules (10.2 item 3) are applied together on every snapshot, so neither the window PID's branch nor
   a launcher that already exited matters: with the JVM's window PID as root the launcher joins by the install-folder
   rule (Branch child), and a JVM whose launcher root is gone is still the tree. Every process's image path is read
   with PROCESS_QUERY_LIMITED_INFORMATION; one whose image cannot be read joins only by descent. The install folder
   is required (the constructor refuses none): without it the tree misses those processes and the survivors check
   proves nothing. A root pid of 0 or less is no root: the tree is the install-folder rule alone (10.2
   `main_frame_missing`), never the descendants of pid 0, which are the whole system.

Verified here: the tree, the kill order, the survivors rule, the metric marshalling and the unreadable paths with a
fake table, and the crash-file scan on real files. Only Phase 0 on a rig verifies the access rights the metric calls
need against the thinkorswim processes, and the soak sampler's numbers against Task Manager (10.11).
"""
from __future__ import annotations

import ctypes
import fnmatch
import os
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..orchestrator.ports import ProcessInfo
from . import ffi

TH32CS_SNAPPROCESS = 0x0002
PROCESS_TERMINATE, PROCESS_VM_READ, PROCESS_QUERY_INFORMATION = 0x0001, 0x0010, 0x0400
PROCESS_QUERY_LIMITED_INFORMATION, SYNCHRONIZE = 0x1000, 0x0010_0000
GR_GDIOBJECTS, GR_USEROBJECTS = 0, 1
KILL_WAIT_MS = 5000
TICKS_PER_S = 10_000_000                        # FILETIME counts 100 ns
CRASH_PATTERNS = ("hs_err_pid*.log", "*.hprof", "*.dmp", "*.mdmp", "*.hdmp")
NEEDED = ("CreateToolhelp32Snapshot", "Process32FirstW", "Process32NextW", "OpenProcess", "CloseHandle",
          "GetProcessMemoryInfo", "GetGuiResources", "GetProcessHandleCount", "GetProcessTimes",
          "QueryFullProcessImageNameW", "TerminateProcess", "WaitForSingleObject")


@dataclass(frozen=True)
class Entry:
    """One row of a Toolhelp process snapshot."""

    pid: int
    ppid: int
    name: str
    threads: int = 0


def descendants(entries: Iterable[Entry], root: int, created: dict[int, int] | None = None) -> list[Entry]:
    """The root's entry and its descendants, breadth first; empty when the root is not in the snapshot (choice 1)."""
    rows = list(entries)
    top = next((e for e in rows if e.pid == root), None)
    if top is None:
        return []
    created = created or {}
    children: dict[int, list[Entry]] = defaultdict(list)
    for e in rows:
        if e.pid != e.ppid:
            children[e.ppid].append(e)
    out, seen, queue = [top], {root}, deque([top])
    while queue:
        parent = queue.popleft()
        for c in children.get(parent.pid, ()):
            born, parent_born = created.get(c.pid), created.get(parent.pid)
            if c.pid in seen or (born is not None and parent_born is not None and born < parent_born):
                continue
            seen.add(c.pid)
            out.append(c)
            queue.append(c)
    return out


def app_tree(entries: Iterable[Entry], roots: Iterable[int], created: dict[int, int] | None = None) -> list[Entry]:
    """10.2 item 3's tree: every process descended from any of `roots` (each root included), each once, in the order
    found (choice 5); the roots are the root pid and the processes running from the install folder."""
    rows = list(entries)
    out: list[Entry] = []
    seen: set[int] = set()
    for root in roots:
        for e in descendants(rows, root, created):
            if e.pid not in seen:
                seen.add(e.pid)
                out.append(e)
    return out


def kill_order(tree: Sequence[ProcessInfo | Entry]) -> list[int]:
    """Children before their parents: deepest first, the roots (processes whose parent is not in the tree) last."""
    parent = {p.pid: p.ppid for p in tree}

    def depth(pid: int) -> int:
        d, seen, up = 0, {pid}, parent.get(pid)
        while up is not None and up in parent and up not in seen:
            seen.add(up)
            d, up = d + 1, parent[up]
        return d

    return [p.pid for p in sorted(tree, key=lambda p: -depth(p.pid))]


def under(path: str | None, folder: str) -> bool:
    """`path` lies inside `folder`, compared case-insensitively with either slash (choice 3)."""
    if not path:
        return False

    def norm(p: str) -> str:
        return p.replace("\\", "/").rstrip("/").lower()

    return norm(path).startswith(norm(folder) + "/")


def find_crash_files(dirs: Iterable[str], since: str, patterns: Sequence[str] = CRASH_PATTERNS) -> tuple[str, ...]:
    """Files matching `patterns` in `dirs` and their subfolders one level down, modified at or after `since`
    (choice 4), sorted."""
    t = datetime.fromisoformat(since)
    if t.tzinfo is None:
        raise ValueError(f"crash_files since {since!r}: the time needs its UTC offset")
    cutoff = t.timestamp()
    found: set[str] = set()
    for d in dirs:
        for folder in [d, *_subfolders(d)]:
            try:
                with os.scandir(folder) as it:
                    for f in it:
                        if (f.is_file() and any(fnmatch.fnmatch(f.name.lower(), p.lower()) for p in patterns)
                                and f.stat().st_mtime >= cutoff):
                            found.add(f.path)
            except OSError:
                continue
    return tuple(sorted(found))


def _subfolders(d: str) -> list[str]:
    try:
        with os.scandir(d) as it:
            return [e.path for e in it if e.is_dir()]
    except OSError:
        return []


class Win32Processes:
    """`ports.ProcessPort` over Win32 (module docstring). `install_dir` is the pinned thinkorswim install folder (the
    tree's install-folder rule and the kill's survivors rule, choice 5; required), `crash_dirs` the folders
    `crash_files` scans; `api` is the function table (`ffi.load` by default, Windows only)."""

    def __init__(self, install_dir: str, crash_dirs: Sequence[str] = (), api: Any = None, *,
                 crash_scan: Callable[[Iterable[str], str], tuple[str, ...]] = find_crash_files):
        if not install_dir:
            raise ValueError("Win32Processes needs the pinned install folder: the process tree's install-folder rule "
                             "and the kill's survivors check depend on it (10.2 item 3, 6.6 step 3)")
        self.install_dir = install_dir
        self.crash_dirs = tuple(crash_dirs)
        self.api = api if api is not None else ffi.load(NEEDED)
        self.crash_scan = crash_scan

    def snapshot(self) -> list[Entry]:
        """Every process of a Toolhelp snapshot."""
        api = self.api
        snap = api.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not ffi.handle_ok(snap):
            raise ffi.fail(api, "CreateToolhelp32Snapshot")
        try:
            e = ffi.PROCESSENTRY32W()
            e.dwSize = ctypes.sizeof(ffi.PROCESSENTRY32W)
            rows: list[Entry] = []
            ok = api.Process32FirstW(snap, e)
            while ok:
                rows.append(Entry(int(e.th32ProcessID), int(e.th32ParentProcessID), ffi.wstr(e.szExeFile),
                                  int(e.cntThreads)))
                ok = api.Process32NextW(snap, e)
            if int(api.get_last_error()) != ffi.ERROR_NO_MORE_FILES:
                raise ffi.fail(api, "Process32NextW")
            return rows
        finally:
            api.CloseHandle(snap)

    def _tree(self, root_pid: int) -> tuple[list[Entry], dict[int, int]]:
        """10.2 item 3's tree on a fresh snapshot (choices 1 and 5): the root's descendants and every process running
        from the install folder with its descendants; and the creation ticks of the processes that could be read."""
        rows = self.snapshot()
        created: dict[int, int] = {}
        from_install: list[int] = []
        for e in rows:
            born, image = self._identity(e.pid)
            if born is not None:
                created[e.pid] = born
            if under(image, self.install_dir):
                from_install.append(e.pid)
        roots = ([root_pid] if root_pid > 0 else []) + from_install
        return app_tree(rows, roots, created), created

    def _identity(self, pid: int) -> tuple[int | None, str | None]:
        """(creation ticks, image path) read with PROCESS_QUERY_LIMITED_INFORMATION; None for what cannot be read."""
        h = self.api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None, None
        try:
            t = self._times(h)
            return (t[0] if t else None), self._image(h)
        finally:
            self.api.CloseHandle(h)

    def _times(self, h: int) -> tuple[int, float] | None:
        """(creation ticks, CPU seconds) from `GetProcessTimes`."""
        created, exited, kernel, user = ffi.FILETIME(), ffi.FILETIME(), ffi.FILETIME(), ffi.FILETIME()
        if not self.api.GetProcessTimes(h, created, exited, kernel, user):
            return None
        return created.ticks, (kernel.ticks + user.ticks) / TICKS_PER_S

    def process_tree(self, root_pid: int) -> tuple[ProcessInfo, ...]:
        entries, _ = self._tree(root_pid)
        return tuple(self._info(e) for e in entries)

    def _info(self, e: Entry) -> ProcessInfo:
        """One process with its metrics (choice 2)."""
        api = self.api
        h = api.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, e.pid)
        if not h:
            return ProcessInfo(e.pid, e.ppid, e.name, threads=e.threads)
        try:
            mem = ffi.PROCESS_MEMORY_COUNTERS_EX()
            mem.cb = ctypes.sizeof(mem)
            mem_ok = bool(api.GetProcessMemoryInfo(h, mem, mem.cb))
            count = ffi.DWORD()
            times = self._times(h)
            return ProcessInfo(e.pid, e.ppid, e.name, exe=self._image(h),
                               working_set=int(mem.WorkingSetSize) if mem_ok else None,
                               private_bytes=int(mem.PrivateUsage) if mem_ok else None,
                               gdi_objects=self._gui(h, GR_GDIOBJECTS), user_objects=self._gui(h, GR_USEROBJECTS),
                               handles=int(count.value) if api.GetProcessHandleCount(h, count) else None,
                               threads=e.threads, cpu_s=times[1] if times else None)
        finally:
            api.CloseHandle(h)

    def _gui(self, h: int, which: int) -> int | None:
        """`GetGuiResources`: zero is a count only when the call left no error (the error is cleared first)."""
        self.api.set_last_error(0)
        n = int(self.api.GetGuiResources(h, which))
        return n if n or not self.api.get_last_error() else None

    def _image(self, h: int) -> str | None:
        buf = ctypes.create_unicode_buffer(1024)
        size = ffi.DWORD(1024)
        return buf.value[:size.value] if self.api.QueryFullProcessImageNameW(h, 0, buf, size) else None

    def kill_tree(self, root_pid: int) -> bool:
        """Kill the tree (choice 5) children first; True when nothing killed and nothing from the install folder
        survives (choice 3)."""
        entries, created = self._tree(root_pid)
        for pid in kill_order(entries):
            h = self.api.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
            if not h:
                continue
            try:
                self.api.TerminateProcess(h, 1)
                self.api.WaitForSingleObject(h, KILL_WAIT_MS)
            finally:
                self.api.CloseHandle(h)
        return not self.survivors({e.pid: created.get(e.pid) for e in entries})

    def survivors(self, killed: dict[int, int | None]) -> list[int]:
        """The pids of a fresh snapshot that survived the kill (choice 3)."""
        out = []
        for e in self.snapshot():
            h = self.api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, e.pid)
            if not h:
                continue
            try:
                t = self._times(h)
                same = e.pid in killed and (killed[e.pid] is None or (t is not None and t[0] == killed[e.pid]))
                if same or under(self._image(h), self.install_dir):
                    out.append(e.pid)
            finally:
                self.api.CloseHandle(h)
        return out

    def crash_files(self, since: str) -> tuple[str, ...]:
        return self.crash_scan(self.crash_dirs, since)
