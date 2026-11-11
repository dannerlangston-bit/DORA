"""Tests for sentinel.win32.access_bridge: the AccessPort over a fake Java Access Bridge."""
import ctypes
import sys

import pytest

from sentinel import jabtree as jt
from sentinel.oracle.sources import EXACT_CONFIDENCE, GroundTruthReader, Reading, extract
from sentinel.orchestrator.ports import AccessNode, AccessPort
from sentinel.win32 import access_bridge as ab
from sentinel.win32 import ffi

MAIN = 0x3000
VM = 7


def put(chars, text):
    raw = text.encode("utf-16-le")
    ctypes.memmove(chars, raw, len(raw))
    chars[len(raw) // 2] = 0


class FakeBridge:
    """A Java accessible tree: id -> (role, name, (x, y, w, h), states, child ids); ids are Java object handles."""

    def __init__(self, contexts, *, root=1, focus=None, unreadable=()):
        self.contexts = contexts
        self.root, self.focus = root, focus
        self.unreadable = set(unreadable)
        self.live: dict[int, int] = {}
        self.released: list[int] = []
        self.ran = 0
        self._next = 1000

    def _acquire(self, ctx_id):
        self._next += 1
        self.live[self._next] = ctx_id
        return self._next

    def Windows_run(self):
        self.ran += 1

    def isJavaWindow(self, hwnd):
        return hwnd == MAIN

    def getAccessibleContextFromHWND(self, hwnd, vm, ac):
        if hwnd != MAIN:
            return 0
        vm.value, ac.value = VM, self._acquire(self.root)
        return 1

    def getAccessibleContextWithFocus(self, hwnd, vm, ac):
        if self.focus is None:
            return 0
        vm.value, ac.value = VM, self._acquire(self.focus)
        return 1

    def getAccessibleContextInfo(self, vm, obj, info):
        assert vm == VM and obj in self.live, "info of a released or unknown object"
        cid = self.live[obj]
        if cid in self.unreadable:
            return 0
        role, name, rect, states, kids = self.contexts[cid]
        put(info.role_en_US, role)
        put(info.role, "localized " + role)
        put(info.name, name)
        put(info.states_en_US, states)
        info.x, info.y, info.width, info.height = rect
        info.childrenCount = len(kids)
        return 1

    def getAccessibleChildFromContext(self, vm, obj, i):
        kids = self.contexts[self.live[obj]][4]
        return self._acquire(kids[i]) if kids[i] is not None else 0

    def releaseJavaObject(self, vm, obj):
        assert obj in self.live, "released twice"
        del self.live[obj]
        self.released.append(obj)


def ticket_tree():
    return {
        1: ("frame", "Main@thinkorswim", (0, 0, 1920, 1040), "enabled,visible,showing", [2, 3]),
        2: ("panel", "", (0, 0, 1920, 40), "visible,showing", [4, 5, None]),
        3: ("canvas", "", (0, 40, 1920, 1000), "visible, showing ", []),          # custom painted: opaque
        4: ("push button", "Buy", (10, 5, 60, 30), "enabled,focusable,visible,showing", []),
        5: ("push button", "Sell", (-1, -1, -1, -1), "enabled", []),             # not showing: no bounds
    }


def bridge(contexts=None, **kw):
    fake = FakeBridge(contexts or ticket_tree(), **{k: v for k, v in kw.items() if k in ("root", "focus",
                                                                                          "unreadable")})
    port = ab.Win32AccessBridge(lambda: MAIN, fake, **{k: v for k, v in kw.items() if k.startswith("max_")})
    return port, fake


def test_it_is_an_access_port_and_runs_the_bridge_once():
    port, fake = bridge()
    assert isinstance(port, AccessPort) and fake.ran == 1
    assert port.available()
    assert not ab.Win32AccessBridge(lambda: None, fake).available()


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="x64 layout")
def test_the_context_info_has_the_bridge_layout():
    assert ctypes.sizeof(ab.AccessibleContextInfo) == 6188
    assert ab.AccessibleContextInfo.role_en_US.offset == 4608 and ab.AccessibleContextInfo.childrenCount.offset == 6148


def test_the_tree_is_read_whole_and_every_java_object_released():
    port, fake = bridge()
    root = port.tree(MAIN)
    assert (root.role, root.name, root.rect) == ("frame", "Main@thinkorswim", (0, 0, 1920, 1040))
    toolbar, canvas = root.children
    buy, sell, gap = toolbar.children
    assert (buy.name, sell.name, gap) == ("Buy", "Sell", jt.UNREAD)           # the null child is a placeholder
    assert buy.states == {"enabled", "focusable", "visible", "showing"} and buy.rect == (10, 5, 60, 30)
    assert sell.rect is None and not sell.opaque
    assert canvas.opaque and canvas.states == {"visible", "showing"} and not toolbar.opaque
    assert fake.live == {} and len(fake.released) == 5


def test_an_unreadable_context_is_a_placeholder_without_its_subtree_and_an_unreadable_root_is_none():
    port, fake = bridge(unreadable={2})
    root = port.tree(MAIN)
    assert root.children[0] == jt.UNREAD and [c.role for c in root.children[1:]] == ["canvas"] and fake.live == {}
    port, fake = bridge(unreadable={1})
    assert port.tree(MAIN) is None and fake.live == {}
    assert port.tree(0x9999) is None                                            # not a Java window


def test_the_walk_is_bounded_in_nodes_and_depth():
    wide = {1: ("table", "grid", (0, 0, 100, 100), "visible", list(range(2, 202)))}
    wide.update({i: ("table cell", str(i), (0, 0, 5, 5), "visible", []) for i in range(2, 202)})
    port, fake = bridge(wide, max_nodes=50)
    assert len(port.tree(MAIN).children) == 49 and fake.live == {}
    deep = {i: ("panel", f"p{i}", (0, 0, 9, 9), "visible", [i + 1]) for i in range(1, 20)}
    deep[20] = ("label", "leaf", (0, 0, 9, 9), "visible", [])
    port, _ = bridge(deep, max_depth=3)
    node, levels = port.tree(MAIN), 0
    while node.children:
        node, levels = node.children[0], levels + 1
    assert levels == 3 and node.name == "p4"


def positions(kids):
    return {1: ("frame", "Main@thinkorswim", (0, 0, 1920, 1040), "visible,showing", [2, 6]),
            2: ("table", "Positions", (0, 40, 1000, 500), "visible,showing", kids),
            3: ("table cell", "AAPL", (0, 40, 100, 20), "visible,showing", []),
            4: ("table cell", "100", (100, 40, 100, 20), "visible,showing", []),
            5: ("table cell", "187.20", (200, 40, 100, 20), "visible,showing", []),
            6: ("text", "1,000.00", (0, 600, 100, 20), "visible,showing", [])}


CELLS = {"qty": "frame[0]/table[0]/table cell[1]", "price": "frame[0]/table[0]/table cell[2]",
         "net_liq": "frame[0]/text[0]"}


def test_a_complete_walk_reads_the_jab_values_exactly():
    port, _ = bridge(positions([3, 4, 5]))
    jab = jt.TreeJabReader(port, lambda: MAIN, CELLS)
    assert [jab.read(f).text for f in CELLS] == ["100", "187.20", "1,000.00"]
    assert jt.dump_from_port(port, MAIN, "1990") is not None


@pytest.mark.parametrize("kids, unreadable", [([3, 4, 5], {4}), ([3, None, 5], set())],
                         ids=["unreadable cell", "null child"])
def test_a_child_the_bridge_did_not_give_never_shifts_a_neighbours_value_into_a_jab_read(kids, unreadable):
    port, fake = bridge(positions(kids), unreadable=unreadable)
    jab = jt.TreeJabReader(port, lambda: MAIN, CELLS)
    assert jab.read("qty") is None and jab.read("price") is None     # the table lost a child: its cells may have moved
    assert jab.read("net_liq") == Reading("net_liq", "jab", "1,000.00", EXACT_CONFIDENCE)   # outside the gap
    got = extract("qty", "int", {"jab": jab, "ocr": GroundTruthReader({"qty": "100"})}, declared="jab")
    assert (got.source, got.text) == ("ocr", "100")
    assert got.fallback.event() == "oracle_source_fallback:qty:jab->ocr"                    # 2.8.1 JAB values
    assert jt.dump_from_port(port, MAIN, "1990") is None             # an incomplete walk is never hashed or diffed
    assert fake.live == {}


def test_focus_owner_is_one_context_without_children():
    port, fake = bridge(focus=4)
    assert port.focus_owner(MAIN) == AccessNode("push button", "Buy", (10, 5, 60, 30),
                                                frozenset({"enabled", "focusable", "visible", "showing"}))
    assert fake.live == {}
    assert bridge()[0].focus_owner(MAIN) is None


@pytest.mark.skipif(sys.platform == "win32", reason="the real table loads on Windows")
def test_without_a_table_it_needs_windows():
    with pytest.raises(ffi.Win32Unavailable):
        ab.Win32AccessBridge(lambda: MAIN)
