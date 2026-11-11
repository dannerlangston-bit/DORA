"""Tests for sentinel.jabtree: the accessible-tree dump, its build-to-build diff (B09, jab_tree_changed) and the OCR
fallback of value cells the tree no longer exposes (2.8.1), on synthetic trees."""
import pytest

from sentinel import jabtree as jt
from sentinel.oracle.sources import GroundTruthReader, extract
from sentinel.orchestrator.ports import AccessNode
from sentinel.orchestrator.results import FindingSpec

VIS = frozenset({"visible", "showing"})


def node(role, name="", *children, rect=(0, 0, 10, 10), states=VIS):
    return AccessNode(role, name, rect, states, tuple(children))


def ticket(buy="Buy", qty="100", extra_button=False, toolbar="Order Entry"):
    buttons = ([node("push button", "Flatten")] if extra_button else []) + [node("push button", buy),
                                                                           node("push button", "Sell")]
    grid = node("table", "Positions", node("table cell", "SPY"), node("table cell", qty),
                node("table cell", "512.25"))
    return node("frame", "Main@thinkorswim", node("panel", toolbar, *buttons), node("panel", "", grid))


def test_the_dump_is_positional_depth_first_and_hashes_identity_only():
    d = jt.dump(ticket(), "1990")
    assert [e.path for e in d.entries][:4] == ["frame[0]", "frame[0]/panel[0]", "frame[0]/panel[0]/push button[0]",
                                              "frame[0]/panel[0]/push button[1]"]
    assert d.by_path()["frame[0]/panel[1]/table[0]/table cell[1]"].name == "100"
    assert d.sha256.startswith("sha256:") and d.sha256 == jt.dump(ticket(), "1991").sha256
    moved = node("frame", "Main@thinkorswim", *(AccessNode(c.role, c.name, (5, 5, 1, 1), frozenset({"focused"}),
                                                           c.children) for c in ticket().children))
    assert jt.dump(moved, "1990").sha256 == d.sha256                          # bounds and states are not identity
    assert jt.dump(ticket(qty="200"), "1990").sha256 != d.sha256


def test_a_buttons_lost_name_is_one_collateral_item_and_a_jab_tree_changed_finding():
    old, new = jt.dump(ticket(), "1990"), jt.dump(ticket(buy=""), "1991")
    d = jt.diff(old, new)
    assert d.changed and d.lost == (jt.LostName("push button", "Buy", "frame[0]/panel[0]/push button[0]"),)
    assert d.gained == () and d.removed_paths == () and d.added_paths == ()
    f = d.finding()
    assert isinstance(f, FindingSpec) and f.kind == "jab_tree_changed"
    assert "build 1991" in f.description and "1 names lost" in f.description
    assert "push button 'Buy' at frame[0]/panel[0]/push button[0]" in f.detail
    assert jt.diff(old, jt.dump(ticket(), "1991")).finding() is None           # the same tree: no finding


def test_a_moved_or_reindexed_control_loses_no_name():
    old = jt.dump(ticket(), "1990")
    shifted = jt.diff(old, jt.dump(ticket(extra_button=True), "1991"))
    assert shifted.changed and shifted.lost == ()
    assert shifted.gained == (jt.LostName("push button", "Flatten", "frame[0]/panel[0]/push button[0]"),)
    assert shifted.added_paths == ("frame[0]/panel[0]/push button[2]",)
    renamed = jt.diff(old, jt.dump(ticket(toolbar="Order Entry Tools"), "1991"))
    assert [x.name for x in renamed.lost] == ["Order Entry"]                    # the panel, not its buttons


def test_a_duplicate_name_reports_the_occurrence_that_changed():
    two = node("frame", "F", node("panel", "A", node("push button", "OK")), node("panel", "B", node("push button",
                                                                                                    "OK")))
    one = node("frame", "F", node("panel", "A", node("push button", "OK")), node("panel", "B", node("push button")))
    (lost,) = jt.diff(jt.dump(two, "1"), jt.dump(one, "2")).lost
    assert lost.path == "frame[0]/panel[1]/push button[0]"


def test_a_removed_value_cell_falls_back_to_ocr():
    old, new_root = jt.dump(ticket(), "1990"), node("frame", "Main@thinkorswim", node("panel", "Order Entry"),
                                                    node("panel", "", node("table", "Positions",
                                                                           node("table cell", "SPY"))))
    new = jt.dump(new_root, "1991")
    cells = {"pos.symbol": "frame[0]/panel[1]/table[0]/table cell[0]",
             "pos.qty": "frame[0]/panel[1]/table[0]/table cell[1]"}
    assert jt.ocr_fallback(cells, new, None) == {"pos.qty"}                    # no earlier build: presence only
    assert jt.ocr_fallback(cells, old, old) == frozenset()
    assert jt.ocr_fallback(cells, new, old) == {"pos.symbol", "pos.qty"}       # the table's cells changed (choice 6)
    assert "frame[0]/panel[1]/table[0]/table cell[1]" in jt.diff(old, new).removed_paths


def test_a_column_inserted_before_a_value_cell_falls_back_to_ocr_though_its_path_still_exists():
    def grid(*cols):
        return node("frame", "Main", node("panel", "Order Entry", node("push button", "Buy")),
                    node("table", "Positions", *(node("table cell", c) for c in cols)))

    base, cand_root = jt.dump(grid("AAPL", "100", "187.20"), "1990"), grid("AAPL", "187.95", "100", "187.20")
    cand = jt.dump(cand_root, "1991")                                           # a Mark column before Qty
    cells = {"qty": "frame[0]/table[0]/table cell[1]", "button": "frame[0]/panel[0]/push button[0]"}
    d = jt.diff(base, cand)
    assert d.changed and d.lost == () and "frame[0]/table[0]/table cell[1]" in cand.by_path()
    marks = jt.ocr_fallback(cells, cand, base)
    assert marks == {"qty"}                                                     # the button's containers are unchanged
    jab = jt.TreeJabReader(FakeAccess(cand_root), lambda: 0x3000, cells, lost=marks)
    got = extract("qty", "int", {"jab": jab, "ocr": GroundTruthReader({"qty": "100"})}, declared="jab")
    assert (got.source, got.text) == ("ocr", "100") and got.fallback.event() == "oracle_source_fallback:qty:jab->ocr"
    unreadable_ocr = GroundTruthReader({"qty": "100"}, confidence=40.0)
    got = extract("qty", "int", {"ocr": unreadable_ocr, "jab": jab}, declared="ocr")
    assert not got.readable                                  # oracle_unreadable, never the Mark value 187.95
    assert jt.ocr_fallback(cells, jt.dump(grid("AAPL", "200", "190.00"), "1992"), base) == frozenset()  # values only


class FakeAccess:
    def __init__(self, root, up=True):
        self.root, self.up, self.trees = root, up, 0

    def available(self):
        return self.up

    def tree(self, hwnd):
        self.trees += 1
        return self.root

    def focus_owner(self, hwnd):
        return None


def test_the_oracle_records_oracle_source_fallback_for_a_cell_the_tree_lost():
    port = FakeAccess(ticket())
    cells = {"pos.qty": "frame[0]/panel[1]/table[0]/table cell[1]",
             "pos.mark": "frame[0]/panel[1]/table[0]/table cell[2]"}
    jab = jt.TreeJabReader(port, lambda: 0x3000, cells, lost={"pos.mark"})
    ocr = GroundTruthReader({"pos.qty": "100", "pos.mark": "512.25"})
    qty = extract("pos.qty", "int", {"jab": jab, "ocr": ocr}, declared="jab")
    assert (qty.source, qty.text, qty.fallback) == ("jab", "100", None)
    mark = extract("pos.mark", "price", {"jab": jab, "ocr": ocr}, declared="jab")
    assert mark.source == "ocr" and mark.text == "512.25"
    assert mark.fallback.event() == "oracle_source_fallback:pos.mark:jab->ocr"
    assert port.trees == 1                                                      # one tree per checkpoint
    jab.refresh()
    port.root = node("frame", "Main@thinkorswim")                               # the cell is gone now
    assert jab.read("pos.qty") is None and port.trees == 2
    assert jab.read("unknown.field") is None


def test_a_lost_child_makes_the_dump_incomplete_and_only_its_siblings_unread():
    gap = node("frame", "Main@thinkorswim", node("panel", "Order Entry", node("push button", "Buy")),
               node("panel", "", node("table", "Positions", node("table cell", "SPY"), jt.UNREAD,
                                      node("table cell", "512.25"))))
    with pytest.raises(jt.IncompleteTree, match="frame\\[0\\]/panel\\[1\\]/table\\[0\\]"):
        jt.dump(gap, "1990")
    port = FakeAccess(gap)
    assert jt.dump_from_port(port, 0x3000, "1990") is None                     # never hashed or diffed (choice 5)
    cells = {"pos.symbol": "frame[0]/panel[1]/table[0]/table cell[0]",
             "pos.qty": "frame[0]/panel[1]/table[0]/table cell[1]", "toolbar": "frame[0]/panel[0]/push button[0]"}
    jab = jt.TreeJabReader(port, lambda: 0x3000, cells)
    assert jab.read("pos.qty") is None and jab.read("pos.symbol") is None     # 512.25 never reads as the qty
    assert jab.read("toolbar").text == "Buy"                                   # outside the context that lost a child
    assert jt.TreeJabReader(FakeAccess(jt.UNREAD), lambda: 0x3000, cells).read("toolbar") is None


def test_a_bridge_that_is_down_reads_nothing():
    assert jt.dump_from_port(FakeAccess(ticket(), up=False), 0x3000, "1990") is None
    jab = jt.TreeJabReader(FakeAccess(ticket(), up=False), lambda: 0x3000, {"f": "frame[0]"})
    assert jab.read("f") is None
    assert jt.TreeJabReader(FakeAccess(ticket()), lambda: None, {"f": "frame[0]"}).read("f") is None


def test_the_stored_dump_round_trips_and_refuses_a_tampered_header():
    d = jt.dump(ticket(qty="1 000 é/x"), "1990")
    assert jt.TreeDump.from_jsonl(d.to_jsonl()) == d
    lines = d.to_jsonl().splitlines()
    lines[2] = lines[2].replace("Main", "Other") if "Main" in lines[2] else lines[2].replace("panel", "label")
    with pytest.raises(ValueError, match="header does not match"):
        jt.TreeDump.from_jsonl("\n".join(lines))
    with pytest.raises(ValueError, match="empty"):
        jt.TreeDump.from_jsonl("")


def test_the_finding_detail_lists_at_most_ten_lost_names():
    many = node("frame", "F", *(node("push button", f"b{i}") for i in range(14)))
    none = node("frame", "F", *(node("push button") for _ in range(14)))
    f = jt.diff(jt.dump(many, "1"), jt.dump(none, "2")).finding()
    assert "14 names lost" in f.description and "(and 4 more)" in f.detail and "b9" in f.detail
    assert "b10" not in f.detail
