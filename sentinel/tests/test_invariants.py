"""Tests for the Invariant Checker (2.7, 12.3, 5.9, 3.10 for invariants, F46)."""
from dataclasses import replace
from pathlib import Path

import pytest

from sentinel.invariants import (
    CALIBRATION_PENDING, ENABLED, INVARIANT_IDS, SEVERITY, STATEMENTS, AAPair, CheckerConfig, GridCell,
    InvariantException, Rect, RequiredControl, RequiredLabel, RuleRegion, ScreenModel, SurfaceRules, TextRegion,
    UIComponent, ValueRegion, Window, WordBox, active_exceptions, check, check_capture, compare, confirm,
    effective_severity, enablement, invariants_on_aa, persist, with_enablement,
)

SPEC_02 = Path(__file__).resolve().parents[2] / "sentinel-spec" / "sections" / "02-testing-model.md"
RULES = SurfaceRules("charts.flexible-grid.active-trader-cell", tier=2)
FRAME = UIComponent("frame", Rect(0, 0, 480, 360), "panel")


def A(*components, size=(480, 360), **kw):
    return ScreenModel("A", size, (FRAME, *components), **kw)


def B(*components, words=(), size=(480, 360), **kw):
    return ScreenModel("B", size, (FRAME, *components), words=tuple(words), **kw)


def ids(model, rules=RULES, **kw):
    return [v.invariant_id for v in check(model, rules, **kw).violations]


def three(model):
    return (model, model, model)


def word(text, x, y, w=30, h=12, conf=92.0, **kw):
    return WordBox(text, Rect(x, y, w, h), conf, **kw)


# --------------------------------------------------------------------------------------------
# The catalog is the spec's


def test_catalog_has_fifteen_invariants():
    assert INVARIANT_IDS == tuple(f"INV-{i:02d}" for i in range(1, 16))
    assert set(SEVERITY) == set(STATEMENTS) == set(INVARIANT_IDS)


@pytest.mark.skipif(not SPEC_02.exists(), reason="spec checkout not present")
def test_catalog_matches_spec_table():
    """Ids, statements and severities equal the 2.7 table."""
    rows = [line.strip().strip("|").split("|") for line in SPEC_02.read_text(encoding="utf-8").splitlines()
            if line.startswith("| INV-")]
    spec = {cells[0].strip(): (cells[1].strip(), cells[5].strip()) for cells in rows}
    assert list(spec) == list(INVARIANT_IDS)
    for inv, (statement, sev) in spec.items():
        assert STATEMENTS[inv] == statement
        assert SEVERITY[inv] == sev


# --------------------------------------------------------------------------------------------
# Worked example (1.11): the ladder overlapping the order strip by 14 px at 480x360


def ladder_a(header_h, strip_y, viewport_y):
    return A(
        UIComponent("header", Rect(0, 0, 480, header_h), "panel", parent="frame"),
        UIComponent("order-strip", Rect(0, strip_y, 480, 30), "panel", parent="frame"),
        UIComponent("ladder-viewport", Rect(0, viewport_y, 480, 352 - viewport_y), "viewport", parent="frame"),
    )


BASE_A = ladder_a(28, 28, 58)          # strip 28..58, viewport 58..352: touching, 0 px gap
CAND_A = ladder_a(22, 22, 38)          # strip 22..52, viewport 38..352: 14 px under the strip


def test_worked_example_branch_a_candidate_only_inv01():
    tier1 = replace(RULES, tier=1)
    assert check_capture(three(BASE_A), tier1).violations == ()
    result = compare(three(BASE_A), three(CAND_A), tier1)
    assert result.invariant_ids == ("INV-01",)
    (r,) = result.reported
    assert r.violation.subject == "ladder-viewport|order-strip"
    assert "480x14" in r.violation.detail
    assert r.severity == "S1" and not r.pre_existing
    assert r.violation.crop == Rect(0, 22, 480, 330)
    assert result.base_only == ()


def ladder_b(header_h, strip_y, viewport_y, price_y):
    btn_y = strip_y + 4
    comps = (
        UIComponent("header", Rect(0, 0, 480, header_h), "panel", parent="frame"),
        UIComponent("order-strip", Rect(0, strip_y, 480, 30), "panel", parent="frame"),
        UIComponent("ladder-viewport", Rect(0, viewport_y, 480, 352 - viewport_y), "panel", parent="frame"),
        UIComponent("buy-mkt", Rect(100, btn_y, 80, 22), "control", parent="order-strip",
                    control_id="buy-mkt-button", confidence=0.97),
        UIComponent("sell-mkt", Rect(200, btn_y, 80, 22), "control", parent="order-strip",
                    control_id="sell-mkt-button", confidence=0.97),
    )
    words = (
        word("BUY", 108, btn_y + 5, anchor="strip.buy.1"), word("MKT", 142, btn_y + 5, anchor="strip.buy.2"),
        word("SELL", 208, btn_y + 5, 32, anchor="strip.sell.1"), word("MKT", 244, btn_y + 5, anchor="strip.sell.2"),
        word("101.25", 110, price_y, 50, conf=90, anchor="ladder.row1.price"),
        word("101.24", 210, price_y, 50, conf=90, anchor="ladder.row1.bid"),
    )
    return B(*comps, words=words)


def test_worked_example_branch_b_segmented_panels_and_word_boxes():
    """Under Branch B: INV-01 by segmented panels, INV-11 for the overlapping word boxes."""
    rules = replace(RULES, tier=1, controls=(RequiredControl("buy-mkt-button"), RequiredControl("sell-mkt-button")))
    base, cand = ladder_b(28, 28, 58, 62), ladder_b(22, 22, 38, 38)
    assert check_capture(three(base), rules).violations == ()
    result = compare(three(base), three(cand), rules)
    assert result.invariant_ids == ("INV-01", "INV-11")
    assert all(r.severity == "S1" and not r.pre_existing for r in result.reported)


# --------------------------------------------------------------------------------------------
# INV-01 sibling bounds intersect


def boxes(a, b, parent_b="frame", **kw):
    return A(UIComponent("a", a, "panel", parent="frame"), UIComponent("b", b, "panel", parent=parent_b, **kw))


def test_inv01_fires_on_real_overlap():
    assert ids(boxes(Rect(0, 0, 100, 100), Rect(50, 50, 100, 100))) == ["INV-01"]
    assert ids(boxes(Rect(0, 0, 100, 100), Rect(90, 90, 100, 100))) == []               # 100 px < 2 percent


@pytest.mark.parametrize("a,b,kw", [
    (Rect(0, 0, 100, 100), Rect(97, 0, 100, 100), {}),          # 3 px on one axis
    (Rect(0, 0, 100, 100), Rect(96, 96, 100, 100), {}),         # 4x4 px is below 2 percent of 10000
    (Rect(0, 0, 100, 100), Rect(50, 50, 100, 100), {"showing": False}),
])
def test_inv01_false_positive_controls(a, b, kw):
    assert ids(boxes(a, b, **kw)) == []


def test_inv01_allowlist_and_non_siblings():
    overlap = (Rect(0, 0, 100, 100), Rect(50, 50, 100, 100))
    assert ids(boxes(*overlap), replace(RULES, allowlist=(("b", "a"),))) == []
    assert ids(boxes(*overlap), replace(RULES, allowlist=(("a", "*"),))) == []
    nested = A(UIComponent("a", Rect(0, 0, 200, 200), "panel", parent="frame"),
               UIComponent("b", Rect(50, 50, 100, 100), "panel", parent="a"))
    assert "INV-01" not in ids(nested)


def test_inv01_branch_b_ignores_text_kinds():
    m = B(UIComponent("t1", Rect(0, 0, 100, 20), "text", parent="frame"),
          UIComponent("t2", Rect(10, 5, 100, 20), "text", parent="frame"))
    assert "INV-01" not in ids(m)


# --------------------------------------------------------------------------------------------
# INV-02 component outside parent


def child_of(parent_rect, child_rect, parent_kind="panel", **pkw):
    return A(UIComponent("p", parent_rect, parent_kind, parent="frame", **pkw),
             UIComponent("c", child_rect, "control", parent="p"))


def test_inv02_child_outside_parent():
    assert ids(child_of(Rect(10, 10, 100, 100), Rect(20, 20, 92, 20))) == ["INV-02"]   # 2 px past the right


def test_inv02_tolerance_and_declared_clipping():
    assert ids(child_of(Rect(10, 10, 100, 100), Rect(20, 20, 91, 20))) == []           # 1 px
    clipped = replace(RULES, clipped_by_design=("c",))
    assert ids(child_of(Rect(10, 10, 100, 100), Rect(20, 20, 120, 20)), clipped) == []


def test_inv02_viewport_excluded_unless_inv06_fires():
    vp = child_of(Rect(10, 10, 100, 100), Rect(10, 10, 90, 400), "viewport", scrollbar_showing=True)
    scrollbar = UIComponent("sb", Rect(100, 10, 10, 100), "scrollbar", parent="p")
    allowed = replace(RULES, allowed_scrollbars={"p": (2000, 2000)})
    with_sb = replace(vp, components=vp.components + (scrollbar,))
    assert ids(with_sb, allowed) == []
    assert ids(with_sb, RULES) == ["INV-02", "INV-06"]                                 # unexpected scrollbar
    no_sb = child_of(Rect(10, 10, 100, 100), Rect(10, 10, 100, 400), "viewport", scrollbar_showing=False)
    assert ids(no_sb) == ["INV-02"]


def test_inv02_branch_b_word_crossing_panel():
    panel = UIComponent("p", Rect(10, 10, 100, 40), "panel", parent="frame")
    assert ids(B(panel, words=[word("Volume", 80, 20, 33)])) == ["INV-02"]             # right edge 113 > 110
    assert ids(B(panel, words=[word("Volume", 60, 20, 33)])) == []


def test_inv02_branch_b_client_rect():
    m = B(UIComponent("c", Rect(460, 10, 30, 20), "control", parent="frame"), size=(480, 360))
    assert ids(m) == ["INV-02"]


# --------------------------------------------------------------------------------------------
# INV-03 text clipped


def named(label, width, box=Rect(10, 10, 100, 20), **kw):
    return UIComponent("lbl", box, "text", parent="frame", label=label, name_width_px=width, insets=(4, 0, 4, 0), **kw)


def test_inv03_branch_a_metrics():
    assert ids(A(named("Buying Power Effect", 120))) == ["INV-03"]
    assert ids(A(named("Qty", 30))) == []


def test_inv03_branch_a_ocr_cross_check():
    m = A(named("Buying Power", 120), words=(word("Buying", 12, 12, 40), word("Power", 56, 12, 40)))
    assert ids(m) == []


def cell_word(text, conf=92.0, **kw):
    cell = UIComponent("cell", Rect(100, 50, 60, 16), "cell", parent="frame")
    return B(cell, words=[WordBox(text, Rect(110, 52, 50, 12), conf, anchor="grid.r1.last", **kw)])


def test_inv03_branch_b_text_differs_from_base():
    assert ids(cell_word("1,234.5"), reference=cell_word("1,234.56")) == ["INV-03"]


def test_inv03_branch_b_false_positive_controls():
    assert ids(cell_word("1,234.56"), reference=cell_word("1,234.56")) == []            # same text
    assert ids(cell_word("1,234.5", oracle_digits=5), reference=cell_word("234.56", oracle_digits=6)) == []
    assert ids(cell_word("1,234.5", conf=60), reference=cell_word("1,234.56")) == []    # below the floor
    assert ids(cell_word("1,234.5")) == []                                              # nothing to compare


def test_inv03_branch_b_strict_prefix_of_declared_label():
    rules = replace(RULES, labels=(RequiredLabel("Bid Size"),))
    assert "INV-03" in ids(cell_word("Si"), rules)
    assert "INV-03" not in ids(cell_word("Bid"), rules)                                 # a whole word of the label


def test_inv03_rule_region_replaces_base_on_masked_live():
    rule = RuleRegion("rgn.ladder.last", Rect(90, 40, 100, 40), "price", decimals=2)
    rules = replace(RULES, data_mode="masked_live", rule_regions=(rule,))
    assert ids(cell_word("101.2"), rules, reference=cell_word("101.2")) == ["INV-03"]   # fails decimals
    assert ids(cell_word("101.25"), rules, reference=cell_word("99.10")) == []         # live value, rule holds
    no_rule = replace(RULES, data_mode="masked_live")
    assert ids(cell_word("101.2"), no_rule, reference=cell_word("101.25")) == []


# --------------------------------------------------------------------------------------------
# INV-04 ellipsis where the base had none


def label_word(text):
    return B(words=[word(text, 20, 20, 60, anchor="hdr.description")])


def test_inv04_branch_b():
    assert ids(label_word("Descrip..."), reference=label_word("Description")) == ["INV-04"]
    assert ids(label_word("Descrip…"), reference=label_word("Description")) == ["INV-04"]


def test_inv04_false_positive_controls():
    assert ids(label_word("Descrip..."), reference=label_word("Descrip...")) == []      # base had one
    assert ids(label_word("Descrip...")) == []                                          # no same anchor
    masked = replace(RULES, data_mode="masked_live")
    assert ids(label_word("Descrip..."), masked, reference=label_word("Description")) == []


def test_inv04_rule_region():
    text_rule = RuleRegion("rgn.desc", Rect(0, 0, 200, 100), "text")
    price_rule = RuleRegion("rgn.desc", Rect(0, 0, 200, 100), "price")
    assert ids(label_word("Descrip..."), replace(RULES, rule_regions=(text_rule,))) == []
    assert ids(label_word("101..."), replace(RULES, rule_regions=(price_rule,))) == ["INV-04"]


def test_inv04_branch_a():
    comp = UIComponent("hdr", Rect(10, 10, 60, 20), "text", parent="frame", text="Descri...", label="Description")
    assert ids(A(comp)) == ["INV-04"]
    assert ids(A(comp), reference=A(replace(comp, text="Descr..."))) == []


# --------------------------------------------------------------------------------------------
# INV-05 required labels


LABELS = replace(RULES, labels=(RequiredLabel("Bid Size", Rect(0, 0, 240, 40)),))


def test_inv05_branch_a():
    present = UIComponent("h", Rect(10, 10, 60, 20), "text", parent="frame", label="Bid Size")
    assert ids(A(present), LABELS) == []
    assert ids(A(), LABELS) == ["INV-05"]
    elsewhere = replace(present, bbox=Rect(300, 200, 60, 20))
    assert ids(A(elsewhere), LABELS) == ["INV-05"]


def test_inv05_branch_b_ocr_within_one_edit_or_template():
    assert ids(B(words=[word("Bid", 10, 10), word("Slze", 44, 10)]), LABELS) == []       # one edit
    assert ids(B(words=[word("Bd", 10, 10), word("Sze", 44, 10)]), LABELS) == ["INV-05"]
    assert ids(B(label_templates={"Bid Size": 0.95}), LABELS) == []
    assert ids(B(label_templates={"Bid Size": 0.85}), LABELS) == ["INV-05"]


# --------------------------------------------------------------------------------------------
# INV-06 unexpected scrollbar


def with_scrollbar(size):
    return A(UIComponent("grid", Rect(0, 0, 300, 200), "viewport", parent="frame"),
             UIComponent("sb", Rect(290, 0, 10, 200), "scrollbar", parent="grid"), size=size)


def test_inv06():
    assert ids(with_scrollbar((480, 360))) == ["INV-06"]                                # not listed
    listed = replace(RULES, allowed_scrollbars={"grid": (400, 300)})
    assert ids(with_scrollbar((480, 360)), listed) == ["INV-06"]                        # none expected this big
    assert ids(with_scrollbar((320, 240)), listed) == []


# --------------------------------------------------------------------------------------------
# INV-07 minimum legible font height


def test_inv07_branch_b_median_cap_height_per_region():
    small = [word("a", 10 + 40 * i, 10, cap_height=5) for i in range(5)]
    assert ids(B(words=small)) == ["INV-07"]
    mixed = [word("a", 10 + 40 * i, 10, cap_height=8) for i in range(4)] + [word("b", 200, 10, cap_height=5)]
    assert ids(B(words=mixed)) == []                                                    # per region, not per word
    icons = small[:4] + [word("x", 300, 10, cap_height=8)]
    icons = [replace(w, icon=True) for w in icons[:4]] + icons[4:]
    assert ids(B(words=icons)) == []


def test_inv07_branch_a_and_calibrated_region_threshold():
    t = UIComponent("t", Rect(10, 10, 60, 20), "text", parent="frame", font_px=8)
    assert ids(A(t)) == ["INV-07"]
    assert ids(A(replace(t, font_px=10))) == []
    region = TextRegion("rgn.small", Rect(0, 0, 100, 100), min_font_px=7.5)
    assert ids(A(t), replace(RULES, text_regions=(region,))) == []


# --------------------------------------------------------------------------------------------
# INV-08 placeholder text in a value region


VALUES = replace(RULES, value_regions=(ValueRegion("rgn.pl", Rect(0, 0, 200, 100)),))


@pytest.mark.parametrize("words", [
    [word("Loading...", 10, 10, 60)], [word("N/A", 10, 10)], [word("--", 10, 10)], [word("NaN", 10, 10)],
    [word("No", 10, 10), word("data", 44, 10)],
])
def test_inv08_fires(words):
    assert ids(B(words=words), VALUES) == ["INV-08"]


def test_inv08_spinner_and_controls():
    assert ids(B(spinners=(Rect(20, 20, 16, 16),)), VALUES) == ["INV-08"]
    assert ids(B(words=[word("101.25", 10, 10, 50)]), VALUES) == []
    assert ids(B(words=[word("N/A", 300, 200)]), VALUES) == []                          # outside the region
    legit = replace(RULES, value_regions=(ValueRegion("rgn.pl", Rect(0, 0, 200, 100), ("N/A",)),))
    assert ids(B(words=[word("N/A", 10, 10)]), legit) == []


# --------------------------------------------------------------------------------------------
# INV-09 windows


SCREEN = Rect(0, 0, 1920, 1080)
MAIN = Window("main", Rect(0, 0, 1920, 1040), "main")


def windows(*ws, pinned=MAIN.rect):
    return A(windows=ws, virtual_screen=SCREEN, pinned_frame_rect=pinned)


def test_inv09():
    assert ids(windows(MAIN)) == []
    assert ids(windows(MAIN, Window("detached", Rect(1800, 100, 400, 300), "top"))) == ["INV-09"]
    owned = Window("dlg", Rect(900, 400, 300, 200), "owned", owner_screen=Rect(0, 0, 1000, 1080))
    assert ids(windows(MAIN, owned)) == ["INV-09"]
    assert ids(windows(replace(MAIN, rect=Rect(10, 0, 1900, 1040)))) == ["INV-09"]      # moved since run start
    foreign = Window("other", Rect(1800, 100, 400, 300), "top", in_process_tree=False)
    assert ids(windows(MAIN, foreign)) == []


def test_inv09_needs_the_virtual_screen():
    with pytest.raises(ValueError):
        check(A(windows=(MAIN,)), RULES)


# --------------------------------------------------------------------------------------------
# INV-10 required controls exactly once


CONTROLS = replace(RULES, controls=(RequiredControl("buy-mkt-button", region=Rect(0, 0, 240, 60)),))


def button(x, conf=0.97):
    return UIComponent(f"buy@{x}", Rect(x, 10, 60, 20), "control", parent="frame", control_id="buy-mkt-button", confidence=conf)


def test_inv10_branch_b():
    assert ids(B(button(10)), CONTROLS) == []
    assert ids(B(), CONTROLS) == ["INV-10"]                                             # erased
    assert ids(B(button(10), button(120)), CONTROLS) == ["INV-10"]                     # twice
    assert ids(B(button(10, conf=0.8)), CONTROLS) == ["INV-10"]                        # below the floor
    assert ids(B(button(300)), CONTROLS) == ["INV-10"]                                 # outside its region


def test_inv10_branch_a_role_and_name():
    rules = replace(RULES, controls=(RequiredControl("send", role="push button", name="Send"),))
    send = UIComponent("send", Rect(10, 10, 60, 20), "control", parent="frame", role="push button", label="Send")
    assert ids(A(send), rules) == []
    assert ids(A(replace(send, label="Cancel")), rules) == ["INV-10"]


# --------------------------------------------------------------------------------------------
# INV-11 text runs overlap


def test_inv11_branch_b():
    assert ids(B(words=[word("101.25", 10, 10, 50), word("BUY", 40, 15)])) == ["INV-11"]
    assert ids(B(words=[word("101.25", 10, 10, 50), word("BUY", 59, 10)])) == []       # 1 px, eroded away
    assert ids(B(words=[word("101.25", 10, 10, 50), word("BUY", 40, 15, conf=50)])) == []


def test_inv11_allowlist():
    tooltip = UIComponent("tooltip", Rect(38, 12, 60, 20), "panel", parent="frame")
    rules = replace(RULES, allowlist=(("tooltip", "*"),))
    m = B(tooltip, words=[word("101.25", 10, 10, 50), word("Tip", 40, 15)])
    assert ids(m, rules) == []


def test_inv11_branch_a():
    t1 = UIComponent("t1", Rect(10, 10, 60, 20), "text", parent="frame")
    t2 = UIComponent("t2", Rect(200, 10, 60, 20), "text", parent="panel-x")
    assert ids(A(t1, t2)) == []
    assert ids(A(t1, replace(t2, bbox=Rect(50, 15, 60, 20)))) == ["INV-11"]


# --------------------------------------------------------------------------------------------
# INV-12 grid columns aligned


GRID = replace(RULES, grid_columns={"last": "right", "symbol": "left"})


def col(name, rights, header=None, wrapped=()):
    cells = [GridCell(name, Rect(r - 40, 20 + 16 * i, 40, 14), wrapped=i in wrapped) for i, r in enumerate(rights)]
    if header:
        cells.append(GridCell(name, header, header=True))
    return A(cells=tuple(cells))


def test_inv12():
    assert ids(col("last", [100, 101, 99]), GRID) == []
    assert ids(col("last", [100, 100, 104]), GRID) == ["INV-12"]
    assert ids(col("last", [100, 104]), GRID) == []                                     # fewer than 3 rows
    assert ids(col("last", [100, 100, 100, 130], wrapped=(3,)), GRID) == []             # wrapped excluded
    assert ids(col("last", [100, 100, 100], header=Rect(70, 0, 30, 16)), GRID) == ["INV-12"]
    assert ids(col("last", [100, 100, 100], header=Rect(58, 0, 42, 16)), GRID) == []


# --------------------------------------------------------------------------------------------
# INV-13 focus traversal


TICKET = replace(RULES, focus_order=("qty", "price", "tif", "send"))


def test_inv13():
    assert ids(A(focus_trace=("qty", "price", "tif", "send", "qty")), TICKET) == []
    assert ids(A(focus_trace=("qty", "tif", "price", "send")), TICKET) == ["INV-13"]
    assert ids(A(focus_trace=("qty", "price")), TICKET) == ["INV-13"]
    assert ids(A(focus_trace=None), TICKET) == []
    assert ids(A(focus_trace=("qty", "tif")), RULES) == []                              # not a ticket flow


# --------------------------------------------------------------------------------------------
# INV-14 error dialog


ERRORS = CheckerConfig(error_title_patterns=(r"(?i)exception", r"(?i)\berror\b"))


def dialog(title, template=True, own=True):
    return windows(MAIN, Window("dlg", Rect(800, 400, 300, 200), "owned", title=title, owner_screen=SCREEN,
                                in_process_tree=own, error_template=template))


def test_inv14():
    assert ids(dialog("java.lang.NullPointerException"), config=ERRORS) == ["INV-14"]
    assert ids(dialog("java.lang.NullPointerException", template=False), config=ERRORS) == []
    assert ids(dialog("What's New"), config=ERRORS) == []                               # title regex required too
    assert ids(dialog("Application Error", own=False), config=ERRORS) == []


def test_inv14_without_patterns_is_loud():
    with pytest.raises(ValueError):
        check(dialog("Application Error"), RULES)


# --------------------------------------------------------------------------------------------
# INV-15 sign and color


PALETTE = CheckerConfig(palette={"positive": (0, 200, 0), "negative": (220, 0, 0), "neutral": (230, 230, 230)})
SIGNED = replace(RULES, signed_columns=frozenset({"pl_open"}))


def pl(text, color, column="pl_open"):
    return A(cells=(GridCell(column, Rect(10, 10, 60, 14), text=text, color=color),))


@pytest.mark.parametrize("text,color", [("+12.50", (215, 10, 5)), ("-3.10", (5, 195, 10)), ("(3.10)", (0, 200, 0)),
                                        ("$1,204.00", (230, 228, 231))])
def test_inv15_fires(text, color):
    assert ids(pl(text, color), SIGNED, config=PALETTE) == ["INV-15"]


@pytest.mark.parametrize("text,color,column", [
    ("+12.50", (0, 200, 0), "pl_open"), ("-3.10", (220, 0, 0), "pl_open"), ("0.00", (220, 0, 0), "pl_open"),
    ("+12.50", (220, 0, 0), "symbol"), ("+12.50", (0, 0, 255), "pl_open"),
])
def test_inv15_controls(text, color, column):
    assert ids(pl(text, color, column), SIGNED, config=PALETTE) == []


def test_inv15_without_palette_is_loud():
    with pytest.raises(ValueError):
        check(pl("+1.00", (0, 200, 0)), SIGNED)


# --------------------------------------------------------------------------------------------
# Common rules: persistence, pre-existing, severity, re-capture, enablement


def test_violation_must_hold_on_all_three_frames():
    bad = boxes(Rect(0, 0, 100, 100), Rect(50, 50, 100, 100))
    good = boxes(Rect(0, 0, 100, 100), Rect(150, 150, 100, 100))
    result = check_capture((bad, bad, good), RULES)
    assert result.violations == ()
    assert [v.invariant_id for v in result.discarded] == ["INV-01"]                     # flake statistics
    assert check_capture(three(bad), RULES).keys == {("INV-01", "a|b")}
    with pytest.raises(ValueError):
        persist([check(bad, RULES)] * 2)


def test_pre_existing_is_reported_once_one_level_lower():
    clipped = A(named("Buying Power Effect", 120, box=Rect(300, 200, 100, 20)))
    both = boxes(Rect(0, 0, 100, 100), Rect(50, 50, 100, 100))
    base = both
    cand = replace(both, components=both.components + clipped.components[1:])
    result = compare(three(base), three(cand), RULES)
    by_id = {r.violation.invariant_id: r for r in result.reported}
    assert by_id["INV-01"].pre_existing and by_id["INV-01"].severity == "S2"
    assert not by_id["INV-03"].pre_existing and by_id["INV-03"].severity == "S1"
    assert result.invariant_ids == ("INV-01", "INV-03")                                 # outcome stays on the candidate


def test_base_only_and_no_base():
    bad = boxes(Rect(0, 0, 100, 100), Rect(50, 50, 100, 100))
    good = boxes(Rect(0, 0, 100, 100), Rect(150, 150, 100, 100))
    fixed = compare(three(bad), three(good), RULES)
    assert not fixed.fired and [v.invariant_id for v in fixed.base_only] == ["INV-01"]
    new = compare(None, three(bad), RULES)
    assert new.fired and not new.reported[0].pre_existing


@pytest.mark.parametrize("inv,tier,pre,expected", [
    ("INV-01", 2, False, "S1"), ("INV-01", 2, True, "S2"), ("INV-04", 2, False, "S2"), ("INV-04", 3, True, "S3"),
    ("INV-04", 1, False, "S1"), ("INV-04", 1, True, "S2"), ("INV-01", 1, True, "S2"), ("INV-01", 1, False, "S1"),
])
def test_effective_severity(inv, tier, pre, expected):
    assert effective_severity(inv, tier=tier, pre_existing=pre) == expected


def test_confirming_recapture():
    bad = boxes(Rect(0, 0, 100, 100), Rect(50, 50, 100, 100))
    good = boxes(Rect(0, 0, 100, 100), Rect(150, 150, 100, 100))
    first = compare(None, three(bad), RULES)
    assert confirm(first, check_capture(three(bad), RULES)).stability == "stable"
    gone = confirm(first, check_capture(three(good), RULES))
    assert gone.stability == "unstable" and gone.confirmed == () and len(gone.unconfirmed) == 1
    unknown = confirm(first, None, cause="budget")
    assert (unknown.stability, unknown.stability_cause, unknown.invariant_ids) == ("unknown", "budget", ("INV-01",))
    with pytest.raises(ValueError):
        confirm(first, None)
    with pytest.raises(ValueError):
        confirm(compare(None, three(good), RULES), None, cause="budget")


def test_calibration_pending_and_exceptions_are_listed_never_silently_off():
    bad = boxes(Rect(0, 0, 100, 100), Rect(50, 50, 100, 100))
    pending = replace(RULES, calibration_pending=frozenset({"INV-01"}))
    fc = check(bad, pending)
    assert fc.violations == () and fc.disabled == ("INV-01",)
    exc = InvariantException("INV-01", "tos-trade-ui", "overlapping by design", "2026-12-01T00:00:00+00:00")
    fc = check(bad, replace(RULES, exceptions=(exc,)))
    assert fc.violations == () and fc.excepted == ("INV-01",)
    live, expired = active_exceptions(replace(RULES, exceptions=(exc,)), "2026-12-02T00:00:00+00:00")
    assert live.exceptions == () and expired == (exc,)


def test_unreadable_bounds_are_unioned_across_frames():
    m = A()
    result = check_capture((m, replace(m, unreadable=("rgn.ladder.bid-column",)), m), RULES)
    assert result.unreadable == ("rgn.ladder.bid-column",)


def test_masks_never_apply():
    """A clipped label inside a masked region is still a violation: the checker takes no masks."""
    import inspect
    assert "mask" not in inspect.signature(check).parameters


def aa(mode, model_a, model_b=None):
    return AAPair(mode, three(model_a), three(model_b or model_a))


def test_enablement_on_frozen_pairs():
    clean = boxes(Rect(0, 0, 100, 100), Rect(150, 150, 100, 100))
    noisy = boxes(Rect(0, 0, 100, 100), Rect(50, 50, 100, 100))
    status = enablement([aa("frozen", clean), aa("frozen", noisy)], RULES)
    assert status["INV-01"] == CALIBRATION_PENDING
    assert status["INV-02"] == ENABLED
    assert with_enablement(RULES, status).calibration_pending == frozenset({"INV-01"})


def test_enablement_masked_live_only_without_frozen_pairs():
    """F46: structural invariants are enabled by masked_live A/A pairs with rule regions in force."""
    live_a, live_b = cell_word("101.25"), cell_word("99.10")
    assert ids(live_a, reference=live_b) == ["INV-03"]                                  # base comparison would fire
    status = enablement([aa("masked_live", live_a, live_b)], RULES)
    assert set(status.values()) == {ENABLED}                                            # masked_live drops it
    noisy = boxes(Rect(0, 0, 100, 100), Rect(50, 50, 100, 100))
    status = enablement([aa("frozen", A()), aa("masked_live", noisy)], RULES)
    assert status["INV-01"] == ENABLED                                                  # frozen pairs decide
    assert set(enablement([], RULES).values()) == {CALIBRATION_PENDING}


def test_enabled_invariant_firing_on_aa_is_a_harness_bug():
    noisy = boxes(Rect(0, 0, 100, 100), Rect(50, 50, 100, 100))
    assert invariants_on_aa(aa("frozen", noisy), RULES) == ("INV-01",)
    pending = replace(RULES, calibration_pending=frozenset({"INV-01"}))
    assert invariants_on_aa(aa("frozen", noisy), pending) == ()


def test_rule_region_formats():
    price = RuleRegion("r", Rect(0, 0, 1, 1), "price", decimals=2, min=0)
    assert price.accepts("1,204.50") and not price.accepts("1204.5") and not price.accepts("-1.00")
    assert RuleRegion("r", Rect(0, 0, 1, 1), "percent", decimals=1).accepts("+2.5%")
    assert RuleRegion("r", Rect(0, 0, 1, 1), "int").accepts("12,000")
    assert RuleRegion("r", Rect(0, 0, 1, 1), "time").accepts("14:21:02")
    assert RuleRegion("r", Rect(0, 0, 1, 1), "text", pattern=r"[A-Z]{1,5}").accepts("SPY")
    with pytest.raises(ValueError):
        RuleRegion("r", Rect(0, 0, 1, 1), "color")


def test_model_validation():
    with pytest.raises(ValueError):
        ScreenModel("C", (10, 10))
    with pytest.raises(ValueError):
        ScreenModel("A", (10, 10), (FRAME, FRAME))
    with pytest.raises(ValueError):
        UIComponent("x", Rect(0, 0, 1, 1), "widget")
    with pytest.raises(ValueError):
        SurfaceRules("s", tier=4)
