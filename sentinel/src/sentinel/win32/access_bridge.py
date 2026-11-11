"""`ports.AccessPort` over the Java Access Bridge, `WindowsAccessBridge-64.dll` (DR-01 Branch A; 2.6, 2.7 bounds, 5.5
rung 1, 5.9, 5.18, 11.8).

- `available()`: the bridge answers `isJavaWindow` for the main frame.
- `tree(hwnd)`: `getAccessibleContextFromHWND`, then depth first `getAccessibleContextInfo` and
  `getAccessibleChildFromContext` for every child, each Java object released (`releaseJavaObject`) once read; the
  result is an `AccessNode` tree (`node_from`, pure: role and states in en_US, name, bounds, opaque).
- `focus_owner(hwnd)`: `getAccessibleContextWithFocus`, the focused context without children (the keyboard-only ticket
  variant, 2.6).

`jabtree` serialises and diffs these trees build to build (B09, 2.8.1).

Choices (reported):

1. Roles and states are the `role_en_US` and `states_en_US` strings ("push button", "visible,showing,enabled"), so a
   localized JRE changes no locator or tree hash; states are split on commas and trimmed.
2. Bounds are `(x, y, width, height)` as the bridge reports them; a context with no positive width and height (the
   bridge reports -1 for a context that is not showing) has `rect` None.
3. Opaque (2.7 Bounds: "custom-painted containers exposing no children are marked opaque"): a context whose role is
   `panel`, `canvas` or `unknown`, with bounds and no children (`OPAQUE_ROLES`).
4. A walk puts at most `MAX_NODES` nodes in the tree and reads `MAX_DEPTH` levels, depth first in child order, so a
   huge grid cannot stall the slow half (a cut-off walk only leaves out the last siblings and the deepest levels, so
   no context read moves). A child the bridge does not give (`getAccessibleChildFromContext` returns null) or whose
   info cannot be read is kept as the `jabtree.UNREAD` placeholder, without its subtree, never dropped: dropping it
   would silently renumber the positional paths of its later siblings (jabtree choices 1 and 5). A root that cannot
   be read is None (the bridge is down for that window).
5. `Windows_run` is called once when the adapter is built; the bridge then needs the calling thread to pump messages
   before it answers, which the rig's slow half does (Phase 0).

Verified here: the `AccessibleContextInfo` layout, the walk, the release of every Java object, the limits and the
unreadable paths, with a fake bridge. Only Phase 0 on a rig verifies the bridge itself: `jabswitch -enable`, the
bundled runtime's `jdk.accessibility` module, which surfaces expose names and values (the DR-01 spike), the
coordinate space of the bounds under PER_MONITOR_AWARE_V2, and the walk's time on the real frames (5.19, 11.8).
"""
from __future__ import annotations

import ctypes
from collections.abc import Callable
from typing import Any

from ..jabtree import UNREAD
from ..orchestrator.ports import AccessNode
from . import ffi

DLL = "WindowsAccessBridge-64"
MAX_STRING_SIZE, SHORT_STRING_SIZE = 1024, 256
JOBJECT64 = ctypes.c_int64
VMID = ffi.LONG
OPAQUE_ROLES = frozenset({"panel", "canvas", "unknown"})
MAX_NODES, MAX_DEPTH = 50_000, 64


class AccessibleContextInfo(ctypes.Structure):
    """AccessBridgePackages.h `AccessibleContextInfo` (wide strings are 2-byte `WCHAR`, ffi choice 1)."""

    _fields_ = [("name", ffi.WCHAR * MAX_STRING_SIZE), ("description", ffi.WCHAR * MAX_STRING_SIZE),
                ("role", ffi.WCHAR * SHORT_STRING_SIZE), ("role_en_US", ffi.WCHAR * SHORT_STRING_SIZE),
                ("states", ffi.WCHAR * SHORT_STRING_SIZE), ("states_en_US", ffi.WCHAR * SHORT_STRING_SIZE),
                ("indexInParent", ffi.INT), ("childrenCount", ffi.INT), ("x", ffi.INT), ("y", ffi.INT),
                ("width", ffi.INT), ("height", ffi.INT), ("accessibleComponent", ffi.BOOL),
                ("accessibleAction", ffi.BOOL), ("accessibleSelection", ffi.BOOL), ("accessibleText", ffi.BOOL),
                ("accessibleInterfaces", ffi.BOOL)]


_P = ctypes.POINTER
PROTOTYPES = {
    "Windows_run": (DLL, None, ()),
    "isJavaWindow": (DLL, ffi.BOOL, (ffi.HWND,)),
    "getAccessibleContextFromHWND": (DLL, ffi.BOOL, (ffi.HWND, _P(VMID), _P(JOBJECT64))),
    "getAccessibleContextWithFocus": (DLL, ffi.BOOL, (ffi.HWND, _P(VMID), _P(JOBJECT64))),
    "getAccessibleContextInfo": (DLL, ffi.BOOL, (VMID, JOBJECT64, _P(AccessibleContextInfo))),
    "getAccessibleChildFromContext": (DLL, JOBJECT64, (VMID, JOBJECT64, ffi.INT)),
    "releaseJavaObject": (DLL, None, (VMID, JOBJECT64)),
}
NEEDED = tuple(PROTOTYPES)


def node_from(info: AccessibleContextInfo, children: tuple[AccessNode, ...] = ()) -> AccessNode:
    """One context as an `AccessNode` (choices 1 to 3)."""
    role = ffi.wstr(info.role_en_US).strip().lower()
    rect = (info.x, info.y, info.width, info.height) if info.width > 0 and info.height > 0 else None
    states = frozenset(s.strip().lower() for s in ffi.wstr(info.states_en_US).split(",") if s.strip())
    opaque = role in OPAQUE_ROLES and rect is not None and info.childrenCount == 0
    return AccessNode(role, ffi.wstr(info.name), rect, states, children, opaque)


class Win32AccessBridge:
    """`ports.AccessPort` over the Java Access Bridge (module docstring). `main_hwnd` gives the main frame's window;
    `api` is the function table (`ffi.load` by default, Windows only)."""

    def __init__(self, main_hwnd: Callable[[], int | None], api: Any = None, *, max_nodes: int = MAX_NODES,
                 max_depth: int = MAX_DEPTH):
        self.main_hwnd = main_hwnd
        self.api = api if api is not None else ffi.load(NEEDED, PROTOTYPES)
        self.max_nodes, self.max_depth = max_nodes, max_depth
        self.api.Windows_run()                                              # choice 5

    def available(self) -> bool:
        hwnd = self.main_hwnd()
        return bool(hwnd) and bool(self.api.isJavaWindow(hwnd))

    def tree(self, hwnd: int) -> AccessNode | None:
        vm, ac = VMID(), JOBJECT64()
        if not self.api.getAccessibleContextFromHWND(hwnd, vm, ac) or not ac.value:
            return None
        budget = [self.max_nodes]
        try:
            return self._node(vm.value, ac.value, 0, budget)
        finally:
            self.api.releaseJavaObject(vm.value, ac.value)

    def _node(self, vm: int, ac: int, depth: int, budget: list[int]) -> AccessNode | None:
        info = AccessibleContextInfo()
        if not self.api.getAccessibleContextInfo(vm, ac, info):
            return None
        budget[0] -= 1
        kids: list[AccessNode] = []
        for i in range(max(0, info.childrenCount) if depth < self.max_depth else 0):
            if budget[0] <= 0:
                break
            child = self.api.getAccessibleChildFromContext(vm, ac, i)
            node = None
            if child:
                try:
                    node = self._node(vm, child, depth + 1, budget)
                finally:
                    self.api.releaseJavaObject(vm, child)
            if node is None:                                                # choice 4: a placeholder, never a gap
                budget[0] -= 1
                node = UNREAD
            kids.append(node)
        return node_from(info, tuple(kids))

    def focus_owner(self, hwnd: int) -> AccessNode | None:
        vm, ac = VMID(), JOBJECT64()
        if not self.api.getAccessibleContextWithFocus(hwnd, vm, ac) or not ac.value:
            return None
        try:
            info = AccessibleContextInfo()
            return node_from(info) if self.api.getAccessibleContextInfo(vm.value, ac.value, info) else None
        finally:
            self.api.releaseJavaObject(vm.value, ac.value)
