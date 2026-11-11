"""Simulated thinkorswim builds: synthetic screens for every planned checkpoint (stand-in for the rig).

A `SimBuild` is a build id plus the visual changes it carries (`VISUAL_CHANGES`). Base and
candidate builds of a run differ only by those changes. `SimApp(build)` renders the screen of a
planned checkpoint with `sentinel.synth` and returns a `Capture`: the frames of the settle loop
(3.5; 250 ms apart, a first frame with a cell flash, then three identical frames, or frames that
never settle for a checkpoint the build lists in `unsettled`), the settled `synth.Screen` and the
bounds-source view of it, an `invariants.ScreenModel`.

The ScreenModel is what DR-01 Branch B would read (2.7 Bounds): panels segmented by their borders,
template-located controls and OCR word boxes. It is built from synth's drawn components, with the
occlusion OCR and template matching would suffer: a control or word painted over by a later
component keeps only its visible part; a word less than half visible is unreadable (dropped) and a
control less than 95 percent visible matches its template below the 0.92 floor. It also carries the grid
header row's cells (INV-03 judges a header word against its cell), the grid cells of INV-12 and INV-15 (each
body cell the word box of its text with the text's dominant color, each column named by its header label) and,
on the stock ticket after the flow's Tab commits, the focus trace of INV-13 (`TABS_BEFORE`, `FOCUS_ORDER`: the
earlier focus owners, then the control the drawn focus ring surrounds).

Screens the example registry asks for (2.7, 12.5): the Active Trader ladder draws its column header row ("Bid
Size", "Price", "Ask Size", the two ladder surfaces' required labels, so the A/A enablement test leaves INV-05 on);
the Position Statement's header row is 14 px, one row of text, and its P/L Open values are drawn in a green and a
red that are each other's red-green channel swap, both within the dark palette's tolerance of its positive and
negative entries (INV-15, M07); the Order Confirmation shows the side toggle's other side (SELL, the label M06
transplants) and each money row's caption raised across its value band's top edge, so the glyph donors of a
swapped digit (10.3 `swapped_digit`) are that value's own digits; the stock ticket draws its keyboard focus ring.

Canary mutants (10.3 Injection). `mutant_capture(base, mutant)` is a canary mutant as the candidate capture of its
checkpoint. What the mutant did not touch is the approved capture's; whatever it touched is read again from its
pixels, as the rig's bounds source and OCR would read them: every word whose pixels changed and every line of
changed pixels goes through `sim_ocr.read_words` (a word read takes the anchor of the word it covers most, so INV-03
and INV-04 compare it with the base word at that anchor, and the screen the Oracle and the Reviewer read carries
it under that word's component); every control and panel whose pixels changed is followed to where its pixels went
(`_relocate`, normalized correlation within `SEARCH_PX`; a control's template score is that correlation, so an
erased or half-covered control falls below the floor; a panel that holds every changed pixel is the change's
container and stays); scrollbars and dialogs the mutant drew are found by their shape (`_scrollbars`,
`_error_dialogs`: the sim's Java exception dialog template is a light body under a one-color title bar with the
round red error icon); grid cells and the focus ring are read again. A `window_offscreen` mutant moves the window
rect, not the pixels (5.7).

Declared regions (choice; 4.5's `invariants` block names no value regions, text regions or column alignments):
`declared_rules(rules, regions_file, base)` adds to a checkpoint's invariant inputs its regions file's rule regions
as INV-08 value regions (a rule region holds a value, 4.8), its regions at most `ONE_ROW_PX` tall as INV-07 text
regions (SD-90: INV-07 is judged per declared one-row text region; without one the whole capture is one region) and
each grid column's alignment as the edge its cells share on the approved capture (INV-12 "alignment per column
type").

Region Registry. `SimApp.registry` turns the config's regions file of a checkpoint (4.8) into a
`diff.regions.RegionsFile` for the synthetic geometry. Region ids, kinds, panels, money-cell
flags, rules and masks come from the config; landmark templates are re-cut from the synthetic base
render (the config's PNGs are captures of the real app); a region the synthetic screen draws as a
component takes that component's rect relative to the landmark, every other region keeps the
config's offset. The Order Entry ticket, the Order Confirmation dialog and the Position Statement
are laid out to the example config's offsets, so their regions are the config's own; the Active
Trader cell keeps synth's worked-example geometry (1.11), which the config's sweep regions file
(strip below the viewport) does not describe, so its two regions are re-derived from components.
A sweep or split step without its own regions file reuses the file of another step of the same
surface and host (the config ships 480x360 only). A checkpoint with no regions file at all is
diffed as one implicit `_rest` region, as 4.8's default says.

Gadget minimum sizes (2.5.1). A build may carry a gadget's minimum size (`SimBuild.minimums`): a sweep step that
requests less renders at the minimum on that axis, the way the real host clamps a gadget, so the sweep driver
measures a clamped size.

Census captures. A census checkpoint captures the surface's host cell or panel at the size that
workspace gives it, not the whole 1920x1080 window: the synthetic workspace has nothing else to
show, and a smaller image keeps the demo fast.

`region_values(screen, regions, located)` is what the Oracle's readers would read in each region: the
text synth drew there (the ground truth `oracle.GroundTruthReader` serves, `pipeline.assertions`), or on a mutant
capture the text read from its pixels.

`observe(base, candidate)` lists what the synthetic build drew differently (the change notes the
simulated Reviewer reads in place of looking at crops); `describe_observations` words them.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from .. import invariants as inv
from .. import sim_ocr
from .. import synth
from ..diff.regions import Landmark, Region, RegionsFile, Rect, FloorException, locate
from ..diff.settle import Frame
from ..scope import PlannedCheckpoint

FRAME_MS = 250                                   # 3.5 frame interval
VIRTUAL_SCREEN = inv.Rect(0, 0, 1920, 1080)      # the rig's display (7.x, 1920x1080 at 100 percent)
UNSETTLED_FRAMES = 64                            # 16 s of frames: past the 15 s max wait (3.5)
SYMBOL = "/MESZ26"                               # fx.rig-01.paper mes_front
OPAQUE_KINDS = frozenset({"window", "panel", "grid_cell", "cell_header", "order_strip", "ladder_viewport", "button",
                          "titlebar", "grid_header", "rect", "splitter"})
PANEL_KINDS = frozenset({"panel", "grid_cell", "cell_header", "order_strip", "ladder_viewport", "grid", "flexgrid",
                         "titlebar", "grid_header"})
CONTROL_KINDS = frozenset({"button", "checkbox"})
WORD_KINDS = frozenset({"label", "text", "cell", "header_cell", "price_cell"})
READ_KINDS = frozenset({"label", "text", "cell", "price_cell"})    # what the Oracle reads in a region
ROW_TOLERANCE = 4                                # px: texts whose centres are this close share a row
OCR_CONFIDENCE = 92.0
TEMPLATE_SCORE = 0.97
STUDY_RGB = (255, 200, 0)                        # the SMA line, the color M24 erases (RGB)
PL_COLORS = {"up": (41, 211, 72), "down": (211, 41, 72)}  # P/L Open: each the other's red-green swap (M07)
FOCUS_ORDER = ("qty-field", "price-field", "confirm-and-send")   # trade.order-entry.stock invariants.focus_order
FOCUS_TOLERANCE = 8      # per channel: the focus ring still reads under M23's +6 on red (global_shift)
TICKET_CONTROLS = {"symbol-box": "ticket.symbol", "qty-field": "ticket.qty.field",
                   "price-field": "ticket.price.field", "confirm-and-send": "ticket.confirm"}
TABS_BEFORE = {"s03": 0, "cp-pre": 0, "s06": 2, "cp-ready": 2}   # ticket state -> Tab commits the flow made so far
ONE_ROW_PX = 24                                  # a declared region this tall holds one row of text (SD-90)
SEARCH_PX = 16                                   # how far a mutant's moved control or panel is looked for
CELL_KINDS = frozenset({"cell", "price_cell", "header_cell"})


@dataclass(frozen=True)
class VisualChange:
    """One visual change a simulated build can carry, and the ticket it implements."""

    change_id: str
    ticket: str | None                          # None: a defect no ticket asked for
    summary: str
    surfaces: frozenset[str]


VISUAL_CHANGES: dict[str, VisualChange] = {c.change_id: c for c in (
    VisualChange("flexgrid-cell-chrome", "TOS-48120",
                 "Flexible Grid cell header 28 to 22 px; hosted content gets the full remaining height, but the "
                 "Active Trader ladder still places its viewport from the old minimum height (the 1.11 defect)",
                 frozenset({"charts.flexible-grid.active-trader-cell", "charts.flexible-grid.chart-cell",
                            "charts.flexible-grid.level2-cell", "charts.flexible-grid.time-sales-cell",
                            "trade.active-trader.ladder"})),
    VisualChange("flexgrid-cell-chrome-fixed", "TOS-48120",
                 "Flexible Grid cell header 28 to 22 px; the ladder viewport starts at the strip's bottom",
                 frozenset({"charts.flexible-grid.active-trader-cell", "charts.flexible-grid.chart-cell",
                            "charts.flexible-grid.level2-cell", "charts.flexible-grid.time-sales-cell",
                            "trade.active-trader.ladder"})),
    VisualChange("chart-header-period", "TOS-48211",
                 "Chart cell header shows the aggregation period after the symbol",
                 frozenset({"charts.flexible-grid.chart-cell"})),
    VisualChange("level2-size-column", "TOS-48305",
                 "Level II cell sizes shown in round lots with an 'x100' suffix",
                 frozenset({"charts.flexible-grid.level2-cell"})),
    VisualChange("bp-effect-house-surcharge", "TOS-48330",
                 "Backend margin table adds a 0.05 percent house surcharge to the buying power effect",
                 frozenset({"dialog.order-confirmation"})),
    VisualChange("confirm-cost-transposed", None,
                 "A pricing refactor transposes two digits of the Order Confirmation cost (15,205.00 for "
                 "100 x 150.25); every other pixel of the dialog is the base's",
                 frozenset({"dialog.order-confirmation"})),
    VisualChange("confirm-account-echo", None,
                 "A logging change echoes the selected account id in the Order Confirmation's top row, right of "
                 "the account fields the redaction anchors cover (a suspected identifier leak, 3.6)",
                 frozenset({"dialog.order-confirmation"})),
)}


@dataclass(frozen=True)
class SimBuild:
    """A simulated build: its id, the visual changes it carries, checkpoints whose frames never settle."""

    build_id: str
    changes: frozenset[str] = frozenset()
    unsettled: frozenset[str] = frozenset()
    minimums: tuple[tuple[str, int, int], ...] = ()     # (surface id, min w, min h): the gadget's minimum size

    def clamp(self, surface_id: str, size: tuple[int, int]) -> tuple[int, int]:
        """The size the build's gadget actually takes for a requested one: never below its minimum (2.5.1)."""
        for sid, w, h in self.minimums:
            if sid == surface_id:
                return max(size[0], w), max(size[1], h)
        return size

    def __post_init__(self) -> None:
        unknown = set(self.changes) - set(VISUAL_CHANGES)
        if unknown:
            raise ValueError(f"unknown visual changes {sorted(unknown)}")

    def has(self, change_id: str) -> bool:
        return change_id in self.changes

    def tickets_for(self, surface_id: str) -> frozenset[str]:
        """Tickets of the changes this build carries that touch `surface_id`."""
        return frozenset(t for c in self.changes if surface_id in VISUAL_CHANGES[c].surfaces
                         and (t := VISUAL_CHANGES[c].ticket) is not None)


@dataclass
class ScreenInfo:
    """What the sim knows about a rendered screen besides its pixels."""

    landmark: str | None                        # the drawn component a landmark template is cut from
    regions: dict[str, str] = field(default_factory=dict)   # region id -> drawn component it covers
    target_window: str = "main"
    layout: dict | None = None                  # Finding.evidence["layout"] for grid-hosted steps
    focus_controls: dict[str, str] = field(default_factory=dict)   # registry control id -> drawn component
    focus_prior: tuple[str, ...] | None = None  # focus owners before this capture's Tab; None: no Tab yet


@dataclass
class Capture:
    """One capture of one checkpoint on one side: settle frames, the screen and its bounds model."""

    checkpoint_id: str
    side: str
    frames: list[Frame]
    screen: synth.Screen
    model: inv.ScreenModel
    info: ScreenInfo

    @property
    def models(self) -> tuple[inv.ScreenModel, ...]:
        """The bounds model of the three settled frames (a static screen reads the same on each)."""
        return (self.model, self.model, self.model)


@dataclass(frozen=True)
class Observation:
    """One thing the candidate build drew differently from the base (3.13 observed_changes shape)."""

    change_type: str
    text: str
    box: tuple[int, int, int, int]
    tickets: frozenset[str]


# ------------------------------------------------------------------------------------------------
# Layouts


def _header_h(build: SimBuild) -> int:
    return 22 if build.has("flexgrid-cell-chrome") or build.has("flexgrid-cell-chrome-fixed") else 28


def _flexcell(size: tuple[int, int], title: str, content: dict, header_h: int) -> dict:
    w, h = size
    return {"type": "window", "name": "cell", "children": [{
        "type": "flexgrid", "name": "grid", "rect": [0, 0, w, h], "col_widths": [w], "row_heights": [h],
        "header_h": header_h, "cells": [{"row": 0, "col": 0, "title": title, "content": content}]}]}


LADDER_REGIONS = {"rgn.ladder.viewport": "ladder.viewport", "rgn.ladder.order-strip": "ladder.strip",
                  "rgn.ladder.qty": "ladder.qty", "rgn.ladder.buy-mkt": "ladder.buy",
                  "rgn.ladder.column-header": "ladder.header", "rgn.ladder.header-ask": "ladder.header.ask.cell"}


def _atcell(size: tuple[int, int], build: SimBuild) -> tuple[dict, ScreenInfo]:
    if build.has("flexgrid-cell-chrome"):
        layout = synth.active_trader_cell_layout(size, "candidate", SYMBOL)
    else:
        layout = synth.active_trader_cell_layout(size, "base", SYMBOL)
        if build.has("flexgrid-cell-chrome-fixed"):
            synth.node(layout, "grid")["header_h"] = 22
    synth.node(layout, "ladder")["column_header"] = True
    info = ScreenInfo("grid.cell00.header.label",
                      dict(LADDER_REGIONS, **{"rgn.atcell.viewport": "ladder.viewport",
                                              "rgn.atcell.qty": "ladder.qty"}),
                      layout={"grid": "1x1", "cell": "(0,0)"})
    return layout, info


def _ladder_window(size: tuple[int, int]) -> tuple[dict, ScreenInfo]:
    layout = {"type": "window", "name": "main", "title": f"Active Trader  {SYMBOL}",
              "children": [{"type": "ladder", "name": "ladder", "column_header": True}]}
    return layout, ScreenInfo("main.title.label", dict(LADDER_REGIONS))


def _watch_rows(rng: np.random.Generator, n: int) -> list[list]:
    symbols = ["AAPL", "MSFT", "NVDA", "SPY", "QQQ", "TSLA", "AMZN", "META", "IWM", "GLD"]
    rows = []
    for s in symbols[:n]:
        last = round(float(rng.uniform(40, 600)) * 4) / 4
        chg = round(float(rng.uniform(-4, 4)), 2)
        rows.append([s, f"{last:.2f}", {"text": f"{chg:+.2f}", "color": "green" if chg >= 0 else "red"}])
    return rows


def _ladder_split(fraction: float) -> tuple[dict, ScreenInfo]:
    side = int(round(960 * fraction))
    w, h = max(960, side + 334), 540             # the ladder keeps room for its order strip
    rows = _watch_rows(np.random.default_rng(11), 10)
    layout = {"type": "window", "name": "main", "title": "Trade", "children": [
        {"type": "panel", "name": "side", "title": "Watchlist", "rect": [0, 0, side, h - 24], "children": [
            {"type": "grid", "name": "side.grid", "rect": [2, 2, max(side - 6, 8), 200],
             "columns": ["Symbol", "Last", "Chg"], "rows": rows}]},
        {"type": "rect", "name": "splitter", "color": "splitter", "rect": [side, 0, 4, h - 24]},
        {"type": "panel", "name": "at", "title": f"Active Trader  {SYMBOL}", "rect": [side + 4, 0, w - side - 4, h - 24],
         "children": [{"type": "ladder", "name": "ladder", "column_header": True}]}]}
    return layout, ScreenInfo("at.title.label", dict(LADDER_REGIONS))


def _chart_cell(size: tuple[int, int], build: SimBuild, symbol: str = "AAPL") -> tuple[dict, ScreenInfo]:
    hh = _header_h(build)
    w, h = size
    cw, ch = w, h - hh
    rng = np.random.default_rng(23)
    axis_w, step, bottom_pad = 54, 36, 8
    children: list[dict] = [{"type": "rect", "name": "chart.plot", "color": "bg", "rect": [0, 0, cw - axis_w, ch]}]
    n_labels = (ch - 10) // step
    for k in range(n_labels):
        y = ch - bottom_pad - k * step - 8
        children.append({"type": "text", "name": f"chart.axis{k}", "text": f"{180 + 2.5 * k:.2f}", "color": "dim",
                         "align": "right", "rect": [cw - axis_w, y, axis_w - 2, 14]})
    price, prices = 0.0, []
    for i in range(max(4, (cw - axis_w - 8) // 9)):
        price += float(rng.normal(0, 3.5))
        prices.append(price)
    lo = min(prices)
    for i, p in enumerate(prices):
        body = int(abs(rng.normal(10, 4))) + 3
        base_y = ch - bottom_pad - 12 - int(round((p - lo) * 2.2)) - body
        up = (i % 3) != 1
        children.append({"type": "rect", "name": f"chart.candle{i}", "color": "green" if up else "red",
                         "rect": [6 + i * 9, max(2, base_y), 6, body]})
        sma = ch - bottom_pad - 20 - int(round((sum(prices[max(0, i - 4):i + 1]) / len(prices[max(0, i - 4):i + 1]) - lo) * 2.2))
        children.append({"type": "rect", "name": f"chart.sma{i}", "color": STUDY_RGB, "rect": [6 + i * 9, max(2, sma), 9, 2]})
    title = f"{symbol}  1D" if build.has("chart-header-period") else symbol
    content = {"type": "window", "name": "chart", "children": children}
    info = ScreenInfo("grid.cell00.header.label", {"rgn.chart.header": "grid.cell00.header", "rgn.chart.plot": "chart.plot"},
                      layout={"grid": "1x1", "cell": "(0,0)"})
    return _flexcell(size, title, content, hh), info


def _data_cell(size: tuple[int, int], build: SimBuild, kind: str) -> tuple[dict, ScreenInfo]:
    hh = _header_h(build)
    w, h = size
    rng = np.random.default_rng(31 if kind == "level2" else 37)
    n = (h - hh - 18) // 16 + 1
    if kind == "level2":
        lots = build.has("level2-size-column")
        cols, widths = ["MMID", "Bid", "Size", "Ask", "Size"], [80, 100, 90, 100, 90]
        rows = []
        for i in range(n):
            sz1, sz2 = int(rng.integers(1, 40)), int(rng.integers(1, 40))
            rows.append([["NSDQ", "ARCA", "EDGX", "BATS", "IEX", "MEMX", "NYSE"][i % 7],
                         f"{180.24 - 0.01 * i:.2f}", f"{sz1}x100" if lots else str(sz1 * 100),
                         f"{180.26 + 0.01 * i:.2f}", f"{sz2}x100" if lots else str(sz2 * 100)])
        title = "Level II  AAPL"
    else:
        cols, widths = ["Time", "Price", "Size", "Exch"], [110, 110, 110, 110]
        rows = []
        for i in range(n):
            px = 180.25 + float(rng.integers(-4, 5)) * 0.01
            rows.append([f"15:29:{59 - i:02d}", {"text": f"{px:.2f}", "color": "green" if px >= 180.25 else "red"},
                         str(int(rng.integers(1, 9)) * 100), ["Q", "P", "K", "Z"][i % 4]])
        title = "Time & Sales  AAPL"
    content = {"type": "grid", "name": "data", "columns": cols, "col_widths": widths, "rows": rows}
    return _flexcell(size, title, content, hh), ScreenInfo("grid.cell00.header.label", {}, layout={"grid": "1x1", "cell": "(0,0)"})


def _landmark_pos(layout: dict, size: tuple[int, int], label: str) -> tuple[int, int]:
    """Top-left of the landmark template (the label's ink box padded by 2 px) in a probe render."""
    x, y, _, _ = synth.render(layout, size).get(label).bbox
    return x - 2, y - 2


def _at(lm: tuple[int, int], origin: tuple[int, int], dx: int, dy: int, w: int, h: int) -> list[int]:
    """A child rect at the config offset (dx, dy) from the landmark, relative to the parent's content box."""
    return [lm[0] + dx - origin[0], lm[1] + dy - origin[1], w, h]


def _order_entry(state: str, build: SimBuild) -> tuple[dict, ScreenInfo]:
    """The stock Order Entry ticket, laid out to the example's s03/s06 regions files (offsets from the title)."""
    size = (640, 200)
    panel = {"type": "panel", "name": "ticket", "title": "Order Entry", "rect": [10, 10, 620, 180], "children": []}
    layout = {"type": "window", "name": "main", "children": [panel]}
    lm = _landmark_pos(layout, size, "ticket.title.label")
    o = (11, 30)                                 # the panel's content box origin
    values = {"census": ("BUY", "", "LMT", "", "DAY"), "s03": ("BUY", "100", "LMT", "", "DAY"),
              "s06": ("BUY", "100", "LMT", "150.25", "DAY"), "cp-ready": ("BUY", "100", "LMT", "150.25", "DAY"),
              "cp-pre": ("BUY", "100", "LMT", "", "DAY")}[state]
    side, qty, otype, price, tif = values
    kids = panel["children"]
    kids.append({"type": "text", "name": "ticket.symbol", "text": "AAPL", "rect": _at(lm, o, 130, 0, 100, 20)})
    for name, (dx, w), text in (("side", (0, 50), side), ("qty", (56, 60), qty), ("type", (122, 50), otype),
                                ("price", (178, 80), price), ("tif", (264, 50), tif)):
        kids.append({"type": "rect", "name": f"ticket.{name}.field", "color": "price", "rect": _at(lm, o, dx, 30, w, 20)})
        if text:
            kids.append({"type": "text", "name": f"ticket.{name}", "text": text,
                         "color": "green" if name == "side" else "text", "rect": _at(lm, o, dx, 30, w, 20)})
    kids.append({"type": "rect", "name": "ticket.divider", "color": "border", "rect": _at(lm, o, 0, 56, 420, 2)})
    for name, dx, text in (("side", 0, "Side"), ("qty", 56, "Qty"), ("type", 122, "Type"), ("price", 178, "Price"),
                           ("tif", 264, "TIF")):
        kids.append({"type": "text", "name": f"ticket.{name}.caption", "text": text, "color": "dim",
                     "rect": _at(lm, o, dx, 64, 56, 16)})
    kids.append({"type": "button", "name": "ticket.confirm", "label": "Confirm and Send", "style": "primary",
                 "rect": _at(lm, o, 300, 64, 120, 24)})
    kids.append({"type": "button", "name": "ticket.advanced", "label": "Advanced Order", "rect": _at(lm, o, 430, 64, 120, 24)})
    tabs = TABS_BEFORE.get(state)
    if tabs is not None:                         # the keyboard focus after the flow's Tab commits (INV-13)
        focused = next(k for k in kids if k["name"] == TICKET_CONTROLS[FOCUS_ORDER[tabs]])
        x, y, w, h = focused["rect"]
        kids.append({"type": "ring", "name": "ticket.focus", "rect": [x - 1, y - 1, w + 2, h + 2]})
    return layout, ScreenInfo("ticket.title.label", focus_controls=dict(TICKET_CONTROLS),
                              focus_prior=FOCUS_ORDER[:tabs] if tabs else None)


def _order_confirmation(build: SimBuild, clock: str) -> tuple[dict, ScreenInfo]:
    """The Order Confirmation dialog, laid out to the example's s07 regions file."""
    size = (700, 270)
    dialog = {"type": "window", "name": "dialog", "title": "Order Confirmation", "children": []}
    lm = _landmark_pos(dialog, size, "dialog.title.label")
    o = (0, 24)
    bp = "-15,032.51" if build.has("bp-effect-house-surcharge") else "-15,025.00"
    cost = "15,205.00" if build.has("confirm-cost-transposed") else "15,025.00"
    kids = dialog["children"]
    kids.append({"type": "text", "name": "confirm.kind", "text": "Limit order, good for the day", "color": "dim",
                 "rect": _at(lm, o, 24, 24, 300, 18)})
    for name, (dx, w), text, color in (("side", (24, 60), "BUY", "green"), ("qty", (90, 48), "100", "text"),
                                       ("symbol", (144, 110), "AAPL", "text"), ("price", (260, 90), "150.25", "text")):
        kids.append({"type": "text", "name": f"confirm.{name}", "text": text, "color": color, "rect": _at(lm, o, dx, 48, w, 20)})
    kids.append({"type": "text", "name": "confirm.reverse", "text": "SELL", "color": "red",
                 "rect": _at(lm, o, 400, 72, 60, 20)})   # the side toggle's other side (M06 transplants it)
    for name, dy, caption, value in (("cost", 96, "Cost", cost), ("bp", 120, "Buying power effect", bp),
                                     ("maxloss", 144, "Max loss", "15,025.00")):
        # the caption sits up across its value band's top edge, so no caption glyph is a glyph of that band (10.3)
        kids.append({"type": "text", "name": f"confirm.{name}.caption", "text": caption, "color": "dim",
                     "rect": _at(lm, o, 24, dy - 9, 200, 16)})
        kids.append({"type": "text", "name": f"confirm.{name}", "text": value, "align": "right", "rect": _at(lm, o, 24, dy, 200, 20)})
    kids.append({"type": "text", "name": "confirm.clock", "text": clock, "color": "dim", "rect": _at(lm, o, 260, 200, 110, 16)})
    if build.has("confirm-account-echo"):
        kids.append({"type": "text", "name": "confirm.echo", "text": EMPTY_ACCOUNT, "color": "dim",
                     "rect": _at(lm, o, 400, 24, 120, 18)})
    for name, dx, label, style in (("send", 380, "Send", "primary"), ("cancel", 480, "Cancel", "default"),
                                   ("delete", 580, "Delete", "default")):
        kids.append({"type": "button", "name": f"confirm.{name}", "label": label, "style": style,
                     "rect": _at(lm, o, dx, 200, 90, 24)})
    return dialog, ScreenInfo("dialog.title.label", target_window="dialog")


EMPTY_ACCOUNT = "D-20481134"              # the fixture's empty account (4.9), the one a PR shard selects
POSITIONS = (("AAPL", 100, 180.25, 1, 181.40), ("/MESZ26", 1, 6210.50, 5, 6195.25), ("CEI", 1000, 0.62, 1, 0.58),
             ("BKNG", 1, 4100.00, 1, 4122.80), (".AAPL261218C200", 1, 12.40, 100, 11.95),
             (".AAPL261218C210", -1, 8.10, 100, 7.60), ("EUR/USD", 10000, 1.0825, 1, 1.0871))


def _position_statement(tick: int, clock: bool) -> tuple[dict, ScreenInfo]:
    """The Position Statement section, laid out to the example's census and cp-positions regions files.
    Marks and P/L are live (`masked_live`): they move with `tick`; the rule regions carry them."""
    size = (640, 210)
    panel = {"type": "panel", "name": "ps", "title": "Position Statement", "rect": [10, 10, 620, 190], "children": []}
    layout = {"type": "window", "name": "main", "children": [panel]}
    lm = _landmark_pos(layout, size, "ps.title.label")
    o = (11, 30)
    rows = []
    for k, (sym, qty, avg, mult, mark0) in enumerate(POSITIONS):
        dec = 4 if mark0 < 2 else 2
        mark = round(mark0 * (1 + 0.0004 * (((tick * 7 + k * 3) % 11) - 5)), dec)
        pl = (mark - avg) * qty * mult            # from the displayed mark, so R08 holds on every row
        rows.append([f"{qty:,}" if qty > 0 else str(qty), f"{avg:,.{dec}f}", f"{mark:,.{dec}f}",
                     {"text": f"{pl:+,.2f}", "color": PL_COLORS["up" if pl >= 0 else "down"]}, sym])
    panel["children"].append({"type": "grid", "name": "ps.grid", "rect": _at(lm, o, 0, 24, 474, 14 + 16 * 7),
                              "columns": ["Qty", "Trade Price", "Mark", "P/L Open", "Instrument"],
                              "col_widths": [62, 94, 94, 104, 120], "header_h": 14, "rows": rows})
    if clock:
        panel["children"].append({"type": "text", "name": "ps.clock", "text": f"10:{31 + tick // 60:02d}:{tick % 60:02d} ET",
                                  "color": "dim", "align": "right", "rect": _at(lm, o, 480, 0, 120, 18)})
    return layout, ScreenInfo("ps.title.label")


# ------------------------------------------------------------------------------------------------
# The bounds-source model (2.7 Bounds, Branch B)


def _ancestors(name: str, by_name: Mapping[str, synth.Drawn]) -> set[str]:
    out: set[str] = set()
    p = by_name[name].parent if name in by_name else None
    while p is not None and p in by_name and p not in out:
        out.add(p)
        p = by_name[p].parent
    return out


def _visible(box: tuple[int, int, int, int], covers: Sequence[tuple[int, int, int, int]]) -> tuple[float, tuple[int, int, int, int] | None]:
    """Visible share of `box` under `covers` and the bounding box of what stays visible."""
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return 0.0, None
    if not covers:
        return 1.0, box
    m = np.ones((h, w), dtype=bool)
    for cx, cy, cw, ch in covers:
        x0, y0, x1, y1 = max(x, cx), max(y, cy), min(x + w, cx + cw), min(y + h, cy + ch)
        if x1 > x0 and y1 > y0:
            m[y0 - y:y1 - y, x0 - x:x1 - x] = False
    share = float(m.mean())
    if share == 0:
        return 0.0, None
    ys, xs = np.nonzero(m)
    return share, (x + int(xs.min()), y + int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))


def _ink_box(d: synth.Drawn) -> tuple[int, int, int, int] | None:
    """The text's ink box: recorded for labels and text, computed for grid and ladder cells as synth aligns them."""
    if d.kind in ("label", "text"):
        return d.bbox
    x, y, w, h = d.bbox
    (tw, th), base = cv2.getTextSize(d.text or "", synth.FONT, synth.SCALE, 1)
    if d.kind == "price_cell":
        align = "center"
    else:
        col = d.name.rsplit("c", 1)[-1] if d.kind == "cell" else d.name.rsplit(".h", 1)[-1]
        align = "left" if col == "0" else "right"
    tx = {"left": 4, "right": w - 4 - tw, "center": (w - tw) // 2}[align]
    ty = (h + th) // 2
    ix, iy, iw, ih = x + tx, y + ty - th - 1, tw, th + base + 1
    x0, y0, x1, y1 = max(ix, x), max(iy, y), min(ix + iw, x + w), min(iy + ih, y + h)
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


def _words(d: synth.Drawn, box: tuple[int, int, int, int]) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Split a text's ink box into word boxes by the font's advance widths."""
    text = d.text or ""
    parts = text.split()
    if len(parts) <= 1:
        return [(text.strip(), box)] if text.strip() else []
    x, y, w, h = box
    out, pos = [], 0
    for p in parts:
        start = text.index(p, pos)
        x0 = cv2.getTextSize(text[:start], synth.FONT, synth.SCALE, 1)[0][0] if start else 0
        pw = cv2.getTextSize(p, synth.FONT, synth.SCALE, 1)[0][0]
        out.append((p, (x + x0, y, min(pw, w - x0), h)))
        pos = start + len(p)
    return [(t, b) for t, b in out if b[2] > 0]


def _fg_color(image: np.ndarray, box: tuple[int, int, int, int]) -> tuple[int, int, int] | None:
    """The dominant foreground color of a text box (INV-15): the median of the pixels farthest from the box's most
    frequent color."""
    x, y, w, h = box
    crop = image[max(0, y):y + h, max(0, x):x + w].reshape(-1, 3).astype(np.int32)
    if crop.size == 0:
        return None
    packed = (crop[:, 0] << 16) | (crop[:, 1] << 8) | crop[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    v = int(values[int(np.argmax(counts))])
    bg = np.array([(v >> 16) & 255, (v >> 8) & 255, v & 255])
    dist = np.abs(crop - bg).max(axis=1)
    if dist.max() == 0:
        return None
    c = np.median(crop[dist >= 0.75 * dist.max()], axis=0).round()
    return int(c[0]), int(c[1]), int(c[2])


def _grid_cells(screen: synth.Screen) -> list[inv.GridCell]:
    """The Branch B grid cells (INV-12, INV-15): each column named by its header label; a body cell is the word box
    of its text with the text's dominant color, a header cell the header slot over the column."""
    comps = screen.components
    cells: list[inv.GridCell] = []
    for g in (d for d in comps if d.kind == "grid"):
        heads = {int(d.name[len(g.name) + 2:]): d for d in comps
                 if d.kind == "header_cell" and d.name.startswith(g.name + ".h")}
        for h in heads.values():
            cells.append(inv.GridCell(str(h.text), inv.Rect(*h.bbox), text=h.text, header=True))
        for d in comps:
            if d.kind != "cell" or not d.name.startswith(g.name + ".r") or not d.text:
                continue
            head = heads.get(int(d.name.rsplit("c", 1)[1]))
            ink = _ink_box(d)
            if head is not None and ink is not None:
                cells.append(inv.GridCell(str(head.text), inv.Rect(*ink), text=d.text,
                                          color=_fg_color(screen.image, ink)))
    return cells


def _focus_owner(ring: tuple[int, int, int, int] | None, info: ScreenInfo,
                 rects: Mapping[str, tuple[int, int, int, int]]) -> str | None:
    """The registry control whose drawn rect holds the focus ring's centre (INV-13 Branch B: the changed rect located
    against the declared control rects)."""
    if ring is None:
        return None
    cx, cy = ring[0] + ring[2] / 2, ring[1] + ring[3] / 2
    for cid, comp in info.focus_controls.items():
        r = rects.get(comp)
        if r is not None and r[0] - 3 <= cx < r[0] + r[2] + 3 and r[1] - 3 <= cy < r[1] + r[3] + 3:
            return cid
    return None


def screen_model(screen: synth.Screen, controls: Mapping[str, str] | None = None,
                 info: ScreenInfo | None = None) -> inv.ScreenModel:
    """The Branch B view of a synthetic screen: segmented panels, template-located controls (matched by
    their OCR text against the registry's controls, `controls` = ocr_text -> control id), OCR words, the grid
    header row's cells (located by the header template; INV-03 judges a header word against its cell), the grid
    cells of INV-12 and INV-15 (`_grid_cells`) and, on a ticket the flow tabbed through (`info.focus_prior`), the
    focus trace: the earlier focus owners and the control the drawn focus ring surrounds (INV-13)."""
    controls = controls or {}
    comps = list(screen.components)
    by_name = {d.name: d for d in comps}
    H, W = screen.image.shape[:2]
    ui: list[inv.UIComponent] = [inv.UIComponent("frame", inv.Rect(0, 0, W, H), "panel")]
    names = {"frame"}
    words: list[inv.WordBox] = []

    def covers_after(i: int, d: synth.Drawn) -> list[tuple[int, int, int, int]]:
        anc = _ancestors(d.name, by_name)
        return [c.bbox for c in comps[i + 1:] if c.kind in OPAQUE_KINDS and c.name not in anc
                and not c.name.startswith(d.name + ".")]

    def parent_of(d: synth.Drawn) -> str:
        p = d.parent
        while p is not None:
            if p in names:
                return p
            p = by_name[p].parent if p in by_name else None
        return "frame"

    is_control = [d.kind in CONTROL_KINDS or (d.kind == "titlebar" and (d.text or "") in controls) for d in comps]
    names |= {d.name for d, c in zip(comps, is_control) if c or (d.kind in PANEL_KINDS and d.parent is not None)}
    for i, d in enumerate(comps):
        if is_control[i]:
            share, _ = _visible(d.bbox, covers_after(i, d))
            cid = controls.get(d.text or "")
            score = TEMPLATE_SCORE * share
            ui.append(inv.UIComponent(d.name, inv.Rect(*d.bbox), "control", parent=parent_of(d), text=d.text,
                                      control_id=cid, confidence=round(score, 3)))
        elif d.kind in PANEL_KINDS and d.parent is not None:
            ui.append(inv.UIComponent(d.name, inv.Rect(*d.bbox), "panel", parent=parent_of(d), text=d.text))
        if d.kind in WORD_KINDS and d.text:
            ink = _ink_box(d)
            if ink is None:
                continue
            cov = covers_after(i, d)
            th = cv2.getTextSize(d.text, synth.FONT, synth.SCALE, 1)[0][1]
            for k, (text, box) in enumerate(_words(d, ink)):
                share, vis = _visible(box, cov)
                if share < 0.5 or vis is None:
                    continue                     # less than half visible: OCR reads nothing above its floor
                words.append(inv.WordBox(text, inv.Rect(*vis), OCR_CONFIDENCE, anchor=f"{d.name}#{k}", cap_height=float(th)))
    ui += [inv.UIComponent(d.name, inv.Rect(*d.bbox), "cell", parent=parent_of(d), text=d.text)
           for d in comps if d.kind == "header_cell"]
    trace = None
    if info is not None and info.focus_prior is not None:
        ring = next((d.bbox for d in comps if d.kind == "focus_ring"), None)
        owner = _focus_owner(ring, info, {d.name: d.bbox for d in comps})
        trace = info.focus_prior + ((owner,) if owner else ())
    main = inv.Window("main", inv.Rect(0, 0, W, H), "main")
    return inv.ScreenModel("B", (W, H), tuple(ui), tuple(words), cells=tuple(_grid_cells(screen)), windows=(main,),
                           virtual_screen=VIRTUAL_SCREEN, pinned_frame_rect=main.rect, focus_trace=trace)


def region_values(screen: synth.Screen, regfile: RegionsFile,
                  located: Mapping[str, tuple[int, int]]) -> dict[str, str | None]:
    """What the Oracle's readers would read in each region of a capture (2.8.1; `pipeline.assertions`):
    the text synth drew whose box centre lies in the region, rows top to bottom joined by newlines and the
    words of a row left to right. A caption drawn inside a value region (`*.caption`, the Order
    Confirmation's "Cost" left of its value) is left out, as the field's OCR whitelist drops it (5.10).
    `located` maps a landmark id to its top-left on this capture; a region whose landmark was not found
    cannot be read at all (None)."""
    texts = [d for d in screen.components if d.kind in READ_KINDS and d.text and not d.name.endswith(".caption")]
    out: dict[str, str | None] = {}
    for r in regfile.regions:
        pos = located.get(r.landmark)
        if pos is None:
            out[r.id] = None
            continue
        x, y = r.rect.x + pos[0], r.rect.y + pos[1]
        inside = [d for d in texts if x <= d.bbox[0] + d.bbox[2] / 2 < x + r.rect.w
                  and y <= d.bbox[1] + d.bbox[3] / 2 < y + r.rect.h]
        rows: list[list[synth.Drawn]] = []
        for d in sorted(inside, key=lambda d: (d.bbox[1] + d.bbox[3] / 2, d.bbox[0])):
            cy = d.bbox[1] + d.bbox[3] / 2
            if rows and abs(cy - (rows[-1][0].bbox[1] + rows[-1][0].bbox[3] / 2)) <= ROW_TOLERANCE:
                rows[-1].append(d)
            else:
                rows.append([d])
        out[r.id] = "\n".join(" ".join((d.text or "").strip() for d in sorted(row, key=lambda d: d.bbox[0]))
                               for row in rows)
    return out


# ------------------------------------------------------------------------------------------------
# Canary mutants: the pixels a mutant changed, read again (10.3 Injection, 2.7 Bounds)


def _overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> int:
    ox = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    oy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return ox * oy if ox > 0 and oy > 0 else 0


def _union(boxes: Sequence[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    return x0, y0, max(b[0] + b[2] for b in boxes) - x0, max(b[1] + b[3] for b in boxes) - y0


def _touched(mask: np.ndarray, box: tuple[int, int, int, int]) -> bool:
    x, y, w, h = box
    return bool(mask[max(0, y):max(0, y + h), max(0, x):max(0, x + w)].any())


def _changed_boxes(changed: np.ndarray, join: bool = True) -> list[tuple[int, int, int, int]]:
    """The boxes of the changed pixels' 8-connected parts; with `join`, parts within 2 px of each other are one."""
    mask = changed.astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        cv2.dilate(mask, np.ones((3, 3), np.uint8)) if join else mask, connectivity=8)
    if not join:
        return [tuple(int(v) for v in stats[k, :4]) for k in range(1, n)]
    ys, xs = np.nonzero(changed)                       # the changed pixels' own extent in each joined part
    ls = labels[ys, xs]
    lo_x, lo_y = np.full(n, changed.shape[1]), np.full(n, changed.shape[0])
    hi_x, hi_y = np.full(n, -1), np.full(n, -1)
    np.minimum.at(lo_x, ls, xs)
    np.minimum.at(lo_y, ls, ys)
    np.maximum.at(hi_x, ls, xs)
    np.maximum.at(hi_y, ls, ys)
    return [(int(lo_x[k]), int(lo_y[k]), int(hi_x[k] - lo_x[k] + 1), int(hi_y[k] - lo_y[k] + 1))
            for k in range(1, n) if hi_x[k] >= 0]


def _row_mode(row: np.ndarray) -> int:
    packed = (row[:, 0].astype(np.int32) << 16) | (row[:, 1].astype(np.int32) << 8) | row[:, 2].astype(np.int32)
    values, counts = np.unique(packed, return_counts=True)
    return int(values[int(np.argmax(counts))])


def _relocate(base: np.ndarray, image: np.ndarray, box: tuple[int, int, int, int]) -> tuple[int, int, float]:
    """Where a control or panel of the base capture sits on the mutant: the displacement within `SEARCH_PX` that
    matches its base pixels best (normalized correlation) and that score; a flat crop has no texture to follow and
    stays, scored by the share of its pixels the mutant kept."""
    H, W = image.shape[:2]
    x, y, w, h = box
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return 0, 0, 1.0
    tmpl = base[y0:y1, x0:x1]
    if float(tmpl.std()) < 1.0:
        return 0, 0, float(np.all(image[y0:y1, x0:x1] == tmpl, axis=2).mean())
    sx0, sy0 = max(0, x0 - SEARCH_PX), max(0, y0 - SEARCH_PX)
    sx1, sy1 = min(W, x1 + SEARCH_PX), min(H, y1 + SEARCH_PX)
    res = cv2.matchTemplate(image[sy0:sy1, sx0:sx1], tmpl, cv2.TM_CCOEFF_NORMED)
    res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
    _, score, _, (bx, by) = cv2.minMaxLoc(res)
    return sx0 + bx - x0, sy0 + by - y0, float(score)


def _scrollbars(image: np.ndarray, changed: np.ndarray, panels: Sequence[inv.UIComponent]) -> list[inv.UIComponent]:
    """Scrollbars drawn where the mutant changed pixels (INV-06, Branch B "scrollbar thumb template"): a track 6 to
    20 px wide and at least three times as tall, of one color, holding a shorter thumb of another color."""
    out = []
    for x, y, w, h in _changed_boxes(changed, join=False):
        if not (6 <= w <= 20 and h >= max(16, 3 * w)):
            continue
        crop = image[y:y + h, x:x + w].astype(np.int16)
        track = crop[:, 0]
        if np.abs(track - track[0]).max() > 2:
            continue                                     # the track is one color top to bottom
        mid = crop[:, w // 2]
        thumb = np.abs(mid - track[0]).max(axis=1) > 30
        rows = np.flatnonzero(thumb)
        if rows.size < 8 or rows.size >= 0.9 * h or rows[-1] - rows[0] + 1 != rows.size:
            continue
        holder = min((p for p in panels if p.bbox.contains_point((x + w / 2, y + h / 2))),
                     key=lambda p: p.bbox.area, default=None)
        out.append(inv.UIComponent(f"scrollbar@{x},{y}", inv.Rect(x, y, w, h), "scrollbar",
                                   parent=holder.name if holder is not None else None))
    return out


def _error_dialogs(image: np.ndarray, changed: np.ndarray, words: Sequence[sim_ocr.OcrWord]) -> list[inv.Window]:
    """Dialogs a mutant composited (INV-14): a light body under a title bar of one color, with the round red error
    icon below the bar (the sim's Java exception dialog template); the title is the OCR of the bar."""
    out = []
    for x, y, w, h in _changed_boxes(changed, join=False):
        if w < 60 or h < 40:
            continue
        crop = image[y:y + h, x:x + w]
        top = _row_mode(crop[1])                     # row 0 is the dialog's frame line
        bar = 1
        while bar < h // 2 and _row_mode(crop[bar]) == top:
            bar += 1
        body = crop[bar + 2:].astype(np.int16)
        if bar < 8 or body.size == 0 or body.mean() < 180:
            continue
        red = (body[..., 0] > 150) & (body[..., 1] < 90) & (body[..., 2] < 90)
        title = " ".join(t.text for t in words if y <= t.bbox[1] + t.bbox[3] / 2 < y + bar
                         and x <= t.bbox[0] + t.bbox[2] / 2 < x + w)
        out.append(inv.Window(f"dialog@{x},{y}", inv.Rect(x, y, w, h), "owned", title=title,
                              owner_screen=VIRTUAL_SCREEN, error_template=int(red.sum()) >= 12))
    return out


def _focus_ring(image: np.ndarray) -> tuple[int, int, int, int] | None:
    """The focus ring's box on a frame: the pixels within `FOCUS_TOLERANCE` of the theme's focus color on every
    channel (a template match tolerates a rendering's small color shift, 10.3 M23)."""
    near = np.abs(image.astype(np.int16) - np.array(synth.THEME["focus"], np.int16)) <= FOCUS_TOLERANCE
    ys, xs = np.nonzero(np.all(near, axis=2))
    if ys.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)


def mutant_capture(base: Capture, mutant: Any) -> Capture:
    """A canary mutant (`canary.Mutant`) as the candidate capture of its checkpoint (10.3 Injection), with the bounds
    model and the screen the simulated OCR and templates read from the mutant's pixels, never from the base's ground
    truth (module docstring, Canary mutants). A `window_offscreen` mutant moves the window, not its pixels: Capture
    grabs the window by its DWM bounds (5.7), so the frame is the base's and the window rect is at the mutant's x."""
    image = np.ascontiguousarray(mutant.image)
    model = base.model
    if mutant.defect_class == "window_offscreen":
        image = base.screen.image
        w0 = model.windows[0]
        x = int(mutant.entry.params.get("x", -200))
        model = dataclasses.replace(model, windows=(dataclasses.replace(w0, rect=inv.Rect(x, w0.rect.y, w0.rect.w,
                                                                                            w0.rect.h)),)
                                    + model.windows[1:])
    changed = np.any(image != base.screen.image, axis=2)
    screen = synth.Screen(image, list(base.screen.components))
    if changed.any():
        model, screen = _reread(base, image, changed, model)
    texts = tuple(d.text for d in screen.components if d.text)
    frames = [Frame(k * FRAME_MS, image, texts=texts) for k in range(3)]
    return Capture(base.checkpoint_id, "candidate", frames, screen, model, base.info)


def _reread(base: Capture, image: np.ndarray, changed: np.ndarray,
            model: inv.ScreenModel) -> tuple[inv.ScreenModel, synth.Screen]:
    """The bounds model and screen of a mutant frame: what the mutant did not touch is the base's; every word,
    control, panel, grid cell and focus ring it touched is read again from its pixels, and the scrollbars and
    dialogs it drew are found (`mutant_capture`)."""
    touched = cv2.dilate(changed.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    removed = [w for w in model.words if _touched(touched, w.bbox.as_tuple())]
    kept = [w for w in model.words if w not in removed]
    read = [o for o in sim_ocr.read_words(image, rects=[w.bbox.as_tuple() for w in removed] + _changed_boxes(changed))
            if not any(k.text == o.text and _overlap(k.bbox.as_tuple(), o.bbox) * 2 > o.bbox[2] * o.bbox[3]
                       for k in kept)]
    # each word read takes the slot (anchor) of the removed word it covers most, so INV-03 and INV-04 compare it
    # with the base word at the same anchor
    slots: dict[int, inv.WordBox] = {}
    taken: set[str] = set()
    for i, o in sorted(enumerate(read), key=lambda t: (t[1].bbox[0], t[1].bbox[1])):
        best = max((w for w in removed if w.anchor not in taken), key=lambda w: _overlap(w.bbox.as_tuple(), o.bbox),
                   default=None)
        if best is not None and _overlap(best.bbox.as_tuple(), o.bbox) > 0:
            slots[i] = best
            taken.add(str(best.anchor))
    words = kept + [inv.WordBox(o.text, inv.Rect(*o.bbox), o.confidence,
                                anchor=slots[i].anchor if i in slots else None, cap_height=o.cap_height)
                    for i, o in enumerate(read)]
    # the screen the Oracle and the Reviewer read: a touched text keeps its name with the words read in its place
    by_comp: dict[str, list[sim_ocr.OcrWord]] = {}
    loose: list[sim_ocr.OcrWord] = []
    for i, o in enumerate(read):
        if i in slots:
            by_comp.setdefault(str(slots[i].anchor).split("#")[0], []).append(o)
        else:
            loose.append(o)
    gone = {str(w.anchor).split("#")[0] for w in removed}
    comps: list[synth.Drawn] = []
    for d in base.screen.components:
        if d.name not in gone:
            comps.append(d)
            continue
        got = sorted(by_comp.get(d.name, []), key=lambda o: (o.baseline, o.bbox[0]))
        if got:
            comps.append(dataclasses.replace(d, text=" ".join(o.text for o in got),
                                             bbox=d.bbox if d.kind in CELL_KINDS else _union([o.bbox for o in got])))
    comps += [synth.Drawn(f"ocr.{o.bbox[0]}.{o.bbox[1]}", "text", o.bbox, None, o.text) for o in loose]
    screen = synth.Screen(image, comps)
    # controls and panels the mutant touched, followed to where their pixels went
    ys, xs = np.nonzero(changed)
    extent = inv.Rect(int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))
    ui = []
    for c in model.components:
        box = c.bbox.as_tuple()
        if c.kind not in ("control", "panel") or (c.kind == "panel" and c.bbox.contains(extent)) \
                or not _touched(changed, box):
            ui.append(c)
            continue
        dx, dy, score = _relocate(base.screen.image, image, box)
        moved = inv.Rect(c.bbox.x + dx, c.bbox.y + dy, c.bbox.w, c.bbox.h) if score >= 0.5 else c.bbox
        ui.append(dataclasses.replace(c, bbox=moved, confidence=round(TEMPLATE_SCORE * max(0.0, score), 3)
                                      if c.kind == "control" else c.confidence))
    ui += _scrollbars(image, changed, [c for c in ui if c.kind == "panel"])
    windows = model.windows + tuple(_error_dialogs(image, changed, read))
    # grid cells: a touched cell is gone; a word read below a column's header slot, inside its grid, is a cell
    cells = [c for c in model.cells if c.header or not _touched(touched, c.bbox.as_tuple())]
    grids = [inv.Rect(*d.bbox) for d in base.screen.components if d.kind == "grid"]
    for o in read:
        cx, cy = o.bbox[0] + o.bbox[2] / 2, o.bbox[1] + o.bbox[3] / 2
        head = next((c for c in model.cells if c.header and c.bbox.x <= cx < c.bbox.right and cy >= c.bbox.bottom
                     and any(g.contains_point((cx, cy)) and g.contains(c.bbox) for g in grids)), None)
        if head is not None:
            cells.append(inv.GridCell(head.column, inv.Rect(*o.bbox), text=o.text, color=_fg_color(image, o.bbox)))
    # the focus ring, where the mutant's pixels show it
    trace = model.focus_trace
    ring = _focus_ring(image)
    if trace is not None and ring != _focus_ring(base.screen.image):
        owner = _focus_owner(ring, base.info, {d.name: d.bbox for d in base.screen.components})
        trace = trace[:-1] + ((owner,) if owner else ())
    return dataclasses.replace(model, components=tuple(ui), words=tuple(words), cells=tuple(cells),
                               windows=windows, focus_trace=trace), screen


def declared_rules(rules: inv.SurfaceRules, regfile: RegionsFile, base: Capture) -> inv.SurfaceRules:
    """The invariant inputs a regions file declares for its checkpoint beyond the registry's (choice in the module
    docstring, Declared regions): its rule regions as INV-08 value regions, its one-row regions as INV-07 text
    regions (SD-90), and each grid column's alignment as the edge its base cells share (INV-12)."""
    placed = locate(regfile, {lm.id: lm.expected for lm in regfile.landmarks if lm.expected is not None})
    rect = {rid: inv.Rect(r.x, r.y, r.w, r.h) for rid, r in placed.items()}
    value = tuple(inv.ValueRegion(r.id, rect[r.id]) for r in regfile.regions if r.type == "rule" and r.id in rect)
    text = tuple(inv.TextRegion(r.id, rect[r.id]) for r in regfile.regions
                 if r.type in ("diff", "freeze", "rule") and r.id in rect and rect[r.id].h <= ONE_ROW_PX)
    columns: dict[str, str] = {}
    for col in dict.fromkeys(c.column for c in base.model.cells):
        body = [c.bbox for c in base.model.cells if c.column == col and not c.header]
        if len(body) < inv.MIN_GRID_ROWS:
            continue
        if max(b.right for b in body) - min(b.right for b in body) <= inv.ALIGN_TOLERANCE_PX:
            columns[col] = "right"
        elif max(b.x for b in body) - min(b.x for b in body) <= inv.ALIGN_TOLERANCE_PX:
            columns[col] = "left"
    return dataclasses.replace(rules, value_regions=value, text_regions=text, grid_columns=columns)


# ------------------------------------------------------------------------------------------------
# Change notes (what the simulated Reviewer reads instead of pixels)


_NOUN = {"cell_header": "cell header", "ladder_viewport": "ladder viewport", "order_strip": "order-control strip",
         "grid": "grid", "titlebar": "title bar"}


def _overlaps(screen: synth.Screen) -> list[tuple[synth.Drawn, synth.Drawn, int]]:
    out = []
    sib = [d for d in screen.components if d.kind in ("order_strip", "ladder_viewport", "cell_header")]
    for i, a in enumerate(sib):
        for b in sib[i + 1:]:
            if a.parent != b.parent:
                continue
            oy = min(a.bbox[1] + a.bbox[3], b.bbox[1] + b.bbox[3]) - max(a.bbox[1], b.bbox[1])
            ox = min(a.bbox[0] + a.bbox[2], b.bbox[0] + b.bbox[2]) - max(a.bbox[0], b.bbox[0])
            if oy > 0 and ox > 0:
                out.append((a, b, oy))
    return out


def observe(base: synth.Screen, cand: synth.Screen, tickets: frozenset[str] = frozenset()) -> list[Observation]:
    """What the candidate drew differently, in the vocabulary of 3.13 `observed_changes`."""
    b = {d.name: d for d in base.components}
    obs: list[Observation] = []
    for d in cand.components:
        o = b.get(d.name)
        if o is None:
            continue
        if d.kind == "cell_header" and o.bbox[3] != d.bbox[3]:
            obs.append(Observation("resized", f"cell header {o.bbox[3]} to {d.bbox[3]} px", d.bbox, tickets))
        elif d.kind in ("ladder_viewport", "order_strip", "grid") and o.bbox[1] != d.bbox[1]:
            dy = o.bbox[1] - d.bbox[1]
            obs.append(Observation("shifted", f"{_NOUN[d.kind]} starts {abs(dy)} px {'higher' if dy > 0 else 'lower'} than base",
                                   d.bbox, tickets))
        elif d.name == "chart.plot" and o.bbox[3] != d.bbox[3]:
            dh = d.bbox[3] - o.bbox[3]
            obs.append(Observation("resized", f"plot area {abs(dh)} px {'taller' if dh > 0 else 'shorter'}", d.bbox, tickets))
        elif d.kind in ("label", "text", "cell") and d.text != o.text and d.text and o.text:
            obs.append(Observation("text_change", f"{o.text!r} now reads {d.text!r}", d.bbox, tickets))
    texts = [o for o in obs if o.change_type == "text_change"]
    if len(texts) > 3:                            # one sentence for a reformatted column, not one per cell
        first = texts[0]
        obs = [o for o in obs if o.change_type != "text_change"]
        obs.append(Observation("text_change", f"{len(texts)} values changed text, for example {first.text}",
                               first.box, tickets))
    for kind, noun in (("price_cell", "price row"), ("row", "grid row")):
        nb = sum(1 for d in base.components if d.kind == kind)
        nc = sum(1 for d in cand.components if d.kind == kind)
        if nc != nb:
            obs.append(Observation("added_element" if nc > nb else "missing_element",
                                   f"{abs(nc - nb)} {'more' if nc > nb else 'fewer'} {noun}{'s' if abs(nc - nb) > 1 else ''}",
                                   (0, 0, cand.image.shape[1], cand.image.shape[0]), tickets))
    axis_b = sum(1 for n in b if n.startswith("chart.axis"))
    axis_c = sum(1 for d in cand.components if d.name.startswith("chart.axis"))
    if axis_c > axis_b:
        obs.append(Observation("added_element", f"{axis_c - axis_b} more price label{'s' if axis_c - axis_b > 1 else ''}",
                               (0, 0, cand.image.shape[1], cand.image.shape[0]), tickets))
    for a, c2, oy in _overlaps(cand):
        obs.append(Observation("overlap", f"{_NOUN[c2.kind]} overlaps the {_NOUN[a.kind]} by {oy} px", c2.bbox, tickets))
    return obs


def describe_observations(obs: Sequence[Observation], ladder: bool = False) -> str:
    """One plain sentence for the Reviewer's `description` (3.13)."""
    if not obs:
        return "No visible difference between the base and candidate crops."
    parts = [o.text for o in obs]
    if ladder:
        parts.append("order controls unchanged")
        if not any(o.change_type == "overlap" for o in obs):
            parts.append("no overlap at this size")
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


# ------------------------------------------------------------------------------------------------
# The app


class SimApp:
    """Renders the screens of one simulated build."""

    def __init__(self, build: SimBuild):
        self.build = build
        self._cache: dict[tuple, Capture] = {}

    # -- screens ---------------------------------------------------------------------------
    def layout(self, cp: PlannedCheckpoint, *, tick: int = 0) -> tuple[dict, tuple[int, int], ScreenInfo]:
        """The synth layout, size and screen facts of a planned checkpoint on this build."""
        s, kind, b = cp.surface_id, cp.test_kind, self.build
        step = dict(cp.size_step or {})
        if s == "charts.flexible-grid.active-trader-cell":
            size = b.clamp(s, (int(step["w"]), int(step["h"]))) if kind == "sweep" else (640, 420)
            layout, info = _atcell(size, b)
            return layout, size, info
        if s == "trade.active-trader.ladder":
            if kind == "sweep":
                size = b.clamp(s, (int(step["w"]), int(step["h"])))
                if step["host"] == "flexgrid":
                    layout, info = _atcell(size, b)
                else:
                    layout, info = _ladder_window(size)
                return layout, size, info
            if kind == "split":
                layout, info = _ladder_split(float(step["fraction"]))
                side = int(round(960 * float(step["fraction"])))
                return layout, (max(960, side + 334), 540), info
            layout, info = _ladder_window((480, 420))
            return layout, (480, 420), info
        if s == "charts.flexible-grid.chart-cell":
            sizes = {"ws.tier-a.swing-trader": (640, 420), "ws.tier-a.options-trader": (480, 420),
                     "ws.tier-a.futures-trader": (480, 360), "ws.tier-a.beginner": (640, 480)}
            size = b.clamp(s, (int(step["w"]), int(step["h"]))) if kind == "sweep" else \
                sizes.get(cp.workspace_id, (640, 420))
            layout, info = _chart_cell(size, b)
            return layout, size, info
        if s in ("charts.flexible-grid.level2-cell", "charts.flexible-grid.time-sales-cell"):
            size = b.clamp(s, (int(step["w"]), int(step["h"]))) if kind == "sweep" else (480, 420)   # 2.5.1
            layout, info = _data_cell(size, b, "level2" if s.endswith("level2-cell") else "time_sales")
            return layout, size, info
        if s == "trade.order-entry.stock":
            state = cp.step_id if kind == "flow" else "census"
            layout, info = _order_entry(state, b)
            return layout, (640, 200), info
        if s == "dialog.order-confirmation":
            layout, info = _order_confirmation(b, f"10:3{1 + tick % 9}:{(17 * tick + 7) % 60:02d}")
            return layout, (700, 270), info
        if s == "monitor.activity.position-statement":
            layout, info = _position_statement(tick, clock=kind == "census")
            return layout, (640, 210), info
        raise KeyError(f"the synthetic app has no screen for {cp.checkpoint_id}")

    def render(self, cp: PlannedCheckpoint, *, tick: int = 0) -> tuple[synth.Screen, ScreenInfo]:
        layout, size, info = self.layout(cp, tick=tick)
        return synth.render(layout, size, seed=7), info

    def capture(self, cp: PlannedCheckpoint, side: str, *, controls: Mapping[str, str] | None = None,
                tick: int = 0) -> Capture:
        """Frames of the settle loop and the settled screen. A frozen checkpoint renders the same pixels
        every time; a `masked_live` one moves its live values with `tick`."""
        tick = tick if cp.data_mode == "masked_live" else 0
        key = (cp.checkpoint_id, side, tick, tuple(sorted((controls or {}).items())))
        if key not in self._cache:            # the same build renders the same pixels: render once
            self._cache[key] = self._capture(cp, side, controls, tick)
        return self._cache[key]

    def _capture(self, cp: PlannedCheckpoint, side: str, controls: Mapping[str, str] | None, tick: int) -> Capture:
        screen, info = self.render(cp, tick=tick)
        img = screen.image
        flash = img.copy()
        h, w = img.shape[:2]
        flash[h // 2:h // 2 + 6, w // 3:w // 3 + 6] = 255   # a cell flash on the first frame
        texts = tuple(t for d in screen.components if d.text for t in [d.text])
        if cp.checkpoint_id in self.build.unsettled:
            frames = [Frame(k * FRAME_MS, flash if k % 2 == 0 else img, texts=texts) for k in range(UNSETTLED_FRAMES)]
        else:
            frames = [Frame(0, flash, texts=texts)] + [Frame(k * FRAME_MS, img, texts=texts) for k in (1, 2, 3)]
        return Capture(cp.checkpoint_id, side, frames, screen, screen_model(screen, controls, info), info)

    # -- the Region Registry entry for the synthetic geometry -------------------------------
    @staticmethod
    def registry(cp: PlannedCheckpoint, base: synth.Screen, info: ScreenInfo, doc: Mapping[str, Any] | None) -> RegionsFile:
        """The checkpoint's regions file (4.8) on the synthetic base render; see the module docstring."""
        if not doc or info.landmark is None:
            return RegionsFile(cp.checkpoint_id)
        lx, ly, _, _ = base.get(info.landmark).bbox
        pos = (lx - 2, ly - 2)
        landmarks = [Landmark(str(lm["id"]), base.crop(info.landmark, 2),
                              confidence_min=float(lm["confidence_min"]) if "confidence_min" in lm else None,
                              expected=pos) for lm in doc.get("landmarks", [])[:1]]
        lm_id = landmarks[0].id if landmarks else None
        regions = []
        for r in doc.get("regions", []):
            comp = info.regions.get(str(r["id"]))
            if comp is not None:
                x, y, w, h = base.get(comp).bbox
                rect = Rect(x - pos[0], y - pos[1], w, h)
            else:
                rr = r["rect"]
                rect = Rect(int(rr["dx"]), int(rr["dy"]), int(rr["w"]), int(rr["h"]))
            fe = r.get("floor_exception")
            regions.append(Region(id=str(r["id"]), type=str(r["type"]), landmark=lm_id or str(r["landmark"]), rect=rect,
                                  panel=str(r.get("panel", "other")), money_cell=bool(r.get("money_cell", False)),
                                  fixture_dependency=r.get("fixture_dependency"),
                                  floor_exception=FloorException.from_dict(fe) if fe else None,
                                  rule=r.get("rule"), mask_ref=r.get("mask_ref")))
        rf = RegionsFile(cp.checkpoint_id, landmarks, regions)
        rf.validate()
        return rf

