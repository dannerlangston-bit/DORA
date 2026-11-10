"""Deterministic, thinkorswim-like synthetic screens with ground truth (tests only; no thinkorswim).

The generator draws dark-theme screens from a small layout description and reports every
component it drew, so detectors (Diff Engine, Invariant Checker, Oracle, canaries) can be tested
against known bounds. Same layout, size and seed give the same pixels on every machine.

Public API:

- `render(layout, size, seed=0, text_gamma=1.0) -> Screen`: draw a layout. A `text_gamma` other
  than 1.0 changes how glyph edges are smoothed everywhere (a global text-rendering change, the
  case of 3.7 granularity 0).
- `Screen`: `image` (H x W x 3 uint8, RGB), `components` (list of `Drawn`), `get(name)`,
  `crop(name, pad=0)`.
- `Drawn(name, kind, bbox, parent, text)`: one drawn component; `bbox` is (x, y, w, h) in image
  pixels, `parent` the enclosing component's name.
- `active_trader_cell_layout(size, build)`: the Active Trader ladder hosted in one Flexible Grid
  cell, with the base or candidate geometry of the worked example (1.11).
- `worked_example(build, seed=0)`: that cell rendered at 480x360. Base: cell header y 0 to 28,
  order strip 28 to 58, ladder viewport 58 to 352 with 14 full price rows, 0 px gap between strip
  and viewport. Candidate: header 22, strip 22 to 52, viewport 38 to 352, overlapping the strip by
  14 px so the first price row draws over the lower half of BUY MKT and SELL MKT.
- `sample_layout(seed=0)`: a 640x400 trading screen (watchlist, position statement with P/L in
  green and red, order entry with Confirm and Send).
- `node(layout, name)`: the layout node with that name (to derive a mutated layout).
- `perturb(image, seed, count, amplitude, rect=None)`: change `count` random pixels by up to
  `amplitude` per channel (A/A noise).

Layout description: nested dicts. Every node has `type`, an optional `name` and an optional
`rect` [x, y, w, h] relative to its parent's content box (default: the whole content box).
Children draw after (on top of) their parent, in order.

| type | fields | components it reports |
|---|---|---|
| `window` | `title`?, `children` | the window; `<name>.title` and `.title.label` when titled (24 px bar) |
| `panel` | `title`?, `children` | the panel; `<name>.title`, `<name>.title.label` (20 px bar) |
| `text` | `text`, `color`?, `align` (left, right, center), `scale`? | `<name>` = ink box |
| `button` | `label`, `style` (default, buy, sell, primary) | `<name>`, `<name>.label` |
| `grid` | `columns`, `rows` (cells are str or {text, color}), `col_widths`?, `row_h` 16, `header_h` 18 | `<name>`, `<name>.header`, `<name>.h<j>`, `<name>.r<i>`, `<name>.r<i>c<j>` |
| `ladder` | `strip_h` 30, `row_h` 21, `bottom_margin` 8, `viewport_min_h`?, `center_price`, `tick` | `<name>.strip`, `.qty`, `.buy`, `.sell`, `.autosend` (+ `.label`s), `.viewport`, `.row<i>`, `.price<i>` |
| `flexgrid` | `col_widths`, `row_heights`, `header_h` 28, `splitter` 4, `cells`: [{row, col, title, content}] | `<name>`, `<name>.cell<r><c>`, `.cell<r><c>.header` (+ `.label`), `.vsplit<j>`, `.hsplit<i>` |
| `rect` | `color` | `<name>` |

Ladder geometry: the strip is at the top of the ladder's rect; the viewport ends `bottom_margin`
px above the rect's bottom. Without `viewport_min_h` (base build) the viewport fills the space
below the strip and draws only full rows. With `viewport_min_h` (candidate build) the viewport is
at least that tall, measured up from its bottom, so it overlaps the strip when the cell is short,
and the last row may be clipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

THEME: dict[str, tuple[int, int, int]] = {
    "bg": (14, 14, 16),
    "panel": (26, 27, 31),
    "border": (58, 60, 66),
    "title": (40, 42, 48),
    "title_text": (214, 214, 214),
    "text": (198, 200, 204),
    "dim": (130, 132, 138),
    "grid_line": (48, 50, 56),
    "header": (36, 38, 44),
    "green": (24, 196, 92),
    "red": (226, 58, 52),
    "button": (62, 64, 72),
    "button_text": (236, 236, 236),
    "buy": (20, 104, 60),
    "sell": (150, 34, 34),
    "primary": (30, 86, 160),
    "bid": (18, 38, 66),
    "ask": (66, 22, 22),
    "price": (32, 33, 38),
    "last": (70, 70, 40),
    "splitter": (70, 72, 80),
    "cell_header": (44, 46, 54),
}
FONT = cv2.FONT_HERSHEY_SIMPLEX
SCALE = 0.36
WINDOW_TITLE_H = 24
PANEL_TITLE_H = 20


@dataclass(frozen=True)
class Drawn:
    """One component the generator drew (ground truth)."""

    name: str
    kind: str
    bbox: tuple[int, int, int, int]
    parent: str | None
    text: str | None = None


@dataclass
class Screen:
    """A rendered screen: RGB pixels plus every component drawn."""

    image: np.ndarray
    components: list[Drawn]

    def get(self, name: str) -> Drawn:
        for c in self.components:
            if c.name == name:
                return c
        raise KeyError(name)

    def crop(self, name: str, pad: int = 0) -> np.ndarray:
        x, y, w, h = self.get(name).bbox
        H, W = self.image.shape[:2]
        return self.image[max(0, y - pad):min(H, y + h + pad), max(0, x - pad):min(W, x + w + pad)].copy()


class _Painter:
    def __init__(self, size: tuple[int, int], seed: int, text_gamma: float):
        w, h = size
        self.img = np.empty((h, w, 3), dtype=np.uint8)
        self.img[:] = THEME["bg"]
        self.rng = np.random.default_rng(seed)
        self.text_gamma = float(text_gamma)
        self.out: list[Drawn] = []
        self.auto = 0

    # -- primitives ------------------------------------------------------------------------
    def color(self, c: Any, default: str) -> tuple[int, int, int]:
        if c is None:
            return THEME[default]
        if isinstance(c, str):
            return THEME[c]
        return (int(c[0]), int(c[1]), int(c[2]))

    def clip(self, r: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
        x, y, w, h = r
        H, W = self.img.shape[:2]
        x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
        if x1 <= x0 or y1 <= y0:
            return None
        return (x0, y0, x1 - x0, y1 - y0)

    def fill(self, r: tuple[int, int, int, int], color: tuple[int, int, int]) -> None:
        c = self.clip(r)
        if c is not None:
            x, y, w, h = c
            self.img[y:y + h, x:x + w] = color

    def frame(self, r: tuple[int, int, int, int], color: tuple[int, int, int]) -> None:
        x, y, w, h = r
        self.fill((x, y, w, 1), color)
        self.fill((x, y + h - 1, w, 1), color)
        self.fill((x, y, 1, h), color)
        self.fill((x + w - 1, y, 1, h), color)

    def text(self, s: str, r: tuple[int, int, int, int], color: tuple[int, int, int], align: str = "left",
             scale: float = SCALE, inset: int = 4) -> tuple[int, int, int, int] | None:
        """Draw `s` inside `r`, clipped to it; returns the clipped ink box."""
        c = self.clip(r)
        if c is None or not s:
            return None
        x, y, w, h = r
        (tw, th), base = cv2.getTextSize(s, FONT, scale, 1)
        tx = {"left": inset, "right": w - inset - tw, "center": (w - tw) // 2}[align]
        ty = (h + th) // 2
        cx, cy, cw, ch = c
        alpha = np.zeros((ch, cw), dtype=np.uint8)
        cv2.putText(alpha, s, (x + tx - cx, y + ty - cy), FONT, scale, 255, 1, cv2.LINE_AA)
        a = alpha.astype(np.float32) / 255.0
        if self.text_gamma != 1.0:
            a = a ** self.text_gamma
        view = self.img[cy:cy + ch, cx:cx + cw]
        view[:] = np.rint(view * (1.0 - a[..., None]) + np.array(color, np.float32) * a[..., None]).astype(np.uint8)
        ink = self.clip((x + tx, y + ty - th - 1, tw, th + base + 1))
        if ink is None:
            return None
        ix, iy, iw, ih = ink
        x0, y0 = max(ix, cx), max(iy, cy)
        x1, y1 = min(ix + iw, cx + cw), min(iy + ih, cy + ch)
        return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None

    def record(self, name: str, kind: str, bbox: tuple[int, int, int, int] | None, parent: str | None,
               text: str | None = None) -> None:
        if bbox is not None:
            c = self.clip(bbox)
            if c is not None:
                self.out.append(Drawn(name, kind, c, parent, text))

    def name(self, node: Mapping[str, Any]) -> str:
        if node.get("name"):
            return str(node["name"])
        self.auto += 1
        return f"{node['type']}{self.auto}"

    # -- nodes -------------------------------------------------------------------------------
    def draw(self, node: Mapping[str, Any], content: tuple[int, int, int, int], parent: str | None) -> None:
        cx, cy, cw, ch = content
        if "rect" in node:
            rx, ry, rw, rh = node["rect"]
            r = (cx + rx, cy + ry, rw, rh)
        else:
            r = content
        name = self.name(node)
        getattr(self, "_" + node["type"])(node, r, name, parent)

    def _children(self, node: Mapping[str, Any], content: tuple[int, int, int, int], name: str) -> None:
        for child in node.get("children", []):
            self.draw(child, content, name)

    def _window(self, node, r, name, parent):
        self.fill(r, THEME["bg"])
        self.record(name, "window", r, parent)
        x, y, w, h = r
        if node.get("title"):
            bar = (x, y, w, WINDOW_TITLE_H)
            self.fill(bar, THEME["title"])
            self.record(f"{name}.title", "titlebar", bar, name, node["title"])
            self.record(f"{name}.title.label", "label", self.text(node["title"], bar, THEME["title_text"]),
                        f"{name}.title", node["title"])
            y, h = y + WINDOW_TITLE_H, h - WINDOW_TITLE_H
        self._children(node, (x, y, w, h), name)

    def _panel(self, node, r, name, parent):
        self.fill(r, THEME["panel"])
        x, y, w, h = r
        if node.get("title"):
            bar = (x, y, w, PANEL_TITLE_H)
            self.fill(bar, THEME["title"])
            self.record(f"{name}.title", "titlebar", bar, name, node["title"])
            self.record(f"{name}.title.label", "label", self.text(node["title"], bar, THEME["title_text"]),
                        f"{name}.title", node["title"])
            y, h = y + PANEL_TITLE_H, h - PANEL_TITLE_H
        self.frame(r, THEME["border"])
        self.record(name, "panel", r, parent, node.get("title"))
        self._children(node, (x + 1, y, w - 2, h - 1), name)

    def _rect(self, node, r, name, parent):
        self.fill(r, self.color(node.get("color"), "panel"))
        self.record(name, "rect", r, parent)

    def _text(self, node, r, name, parent):
        ink = self.text(str(node["text"]), r, self.color(node.get("color"), "text"), node.get("align", "left"),
                        float(node.get("scale", SCALE)))
        self.record(name, "text", ink, parent, str(node["text"]))

    def _button(self, node, r, name, parent):
        style = node.get("style", "default")
        bg = THEME["button" if style == "default" else style]
        self.fill(r, bg)
        self.frame(r, tuple(min(255, v + 40) for v in bg))
        self.record(name, "button", r, parent, node["label"])
        self.record(f"{name}.label", "label", self.text(node["label"], r, THEME["button_text"], "center"),
                    name, node["label"])

    def _grid(self, node, r, name, parent):
        x, y, w, h = r
        cols: Sequence[str] = node["columns"]
        widths = node.get("col_widths") or [w // len(cols)] * len(cols)
        row_h, head_h = int(node.get("row_h", 16)), int(node.get("header_h", 18))
        self.record(name, "grid", r, parent)
        head = (x, y, w, head_h)
        self.fill(head, THEME["header"])
        self.record(f"{name}.header", "grid_header", head, name)
        xs = [x + sum(widths[:j]) for j in range(len(cols))]
        for j, label in enumerate(cols):
            cell = (xs[j], y, widths[j], head_h)
            self.record(f"{name}.h{j}", "header_cell", cell, f"{name}.header", label)
            self.text(label, cell, THEME["dim"], "left" if j == 0 else "right")
        for i, row in enumerate(node.get("rows", [])):
            ry = y + head_h + i * row_h
            if ry + row_h > y + h:
                break
            self.record(f"{name}.r{i}", "row", (x, ry, w, row_h), name)
            for j, cell_val in enumerate(row):
                val = cell_val if isinstance(cell_val, Mapping) else {"text": cell_val}
                cell = (xs[j], ry, widths[j], row_h)
                self.text(str(val["text"]), cell, self.color(val.get("color"), "text"), "left" if j == 0 else "right")
                self.record(f"{name}.r{i}c{j}", "cell", cell, f"{name}.r{i}", str(val["text"]))
            self.fill((x, ry + row_h - 1, w, 1), THEME["grid_line"])

    def _ladder(self, node, r, name, parent):
        x, y, w, h = r
        strip_h, row_h = int(node.get("strip_h", 30)), int(node.get("row_h", 21))
        bottom = y + h - int(node.get("bottom_margin", 8))
        strip = (x, y, w, strip_h)
        self.fill(strip, THEME["panel"])
        self.record(f"{name}.strip", "order_strip", strip, parent)
        by, bh = y + 4, strip_h - 8
        parts = [("qty", (x + 6, by, 48, bh), "1", "default"), ("buy", (x + 60, by, 80, bh), "BUY MKT", "buy"),
                 ("sell", (x + 146, by, 80, bh), "SELL MKT", "sell")]
        for key, rect, label, style in parts:
            self._button({"label": label, "style": style}, rect, f"{name}.{key}", f"{name}.strip")
        auto = (x + 232, by, max(0, min(84, x + w - (x + 232))), bh)
        self.frame((auto[0], auto[1] + (bh - 10) // 2, 10, 10), THEME["dim"])
        self.record(f"{name}.autosend", "checkbox", auto, f"{name}.strip", "Auto send")
        self.record(f"{name}.autosend.label", "label",
                    self.text("Auto send", (auto[0] + 12, auto[1], auto[2] - 12, bh), THEME["text"], inset=2),
                    f"{name}.autosend", "Auto send")
        min_h = node.get("viewport_min_h")
        if min_h is None:
            top, full_only = y + strip_h, True
        else:
            top, full_only = bottom - max(int(min_h), bottom - (y + strip_h)), False
        vp = (x, top, w, bottom - top)
        self.fill(vp, THEME["price"])
        self.record(f"{name}.viewport", "ladder_viewport", vp, parent)
        n = (bottom - top) // row_h if full_only else -(-(bottom - top) // row_h)
        fr = [0.14, 0.17, 0.17, 0.18, 0.17, 0.17]
        cx = [x + int(round(w * sum(fr[:k]))) for k in range(len(fr) + 1)]
        center = float(node.get("center_price", 4500.25))
        tick = float(node.get("tick", 0.25))
        mid = n // 2
        for i in range(n):
            ry = top + i * row_h
            rh = min(row_h, bottom - ry)
            price = center + (mid - i) * tick
            self.fill((cx[1], ry, cx[3] - cx[1], rh), THEME["bid"])
            self.fill((cx[4], ry, cx[6] - cx[4], rh), THEME["ask"])
            self.fill((cx[3], ry, cx[4] - cx[3], rh), THEME["last" if i == mid else "price"])
            label = f"{price:.2f}"
            self.text(label, (cx[3], ry, cx[4] - cx[3], row_h), THEME["text"], "center")
            size = str(int(self.rng.integers(1, 60)))
            if i > mid:
                self.text(size, (cx[2], ry, cx[3] - cx[2], row_h), THEME["text"], "right")
            elif i < mid:
                self.text(size, (cx[4], ry, cx[5] - cx[4], row_h), THEME["text"], "left")
            self.fill((x, ry + row_h - 1, w, 1), THEME["grid_line"])
            self.record(f"{name}.row{i}", "price_row", (x, ry, w, rh), f"{name}.viewport")
            self.record(f"{name}.price{i}", "price_cell", (cx[3], ry, cx[4] - cx[3], rh), f"{name}.row{i}", label)
        # anything below the viewport (the bottom margin) stays panel colored
        self.fill((x, bottom, w, y + h - bottom), THEME["panel"])

    def _flexgrid(self, node, r, name, parent):
        x, y, w, h = r
        sp, head_h = int(node.get("splitter", 4)), int(node.get("header_h", 28))
        widths, heights = list(node["col_widths"]), list(node["row_heights"])
        self.record(name, "flexgrid", r, parent)
        xs = [x + sum(widths[:j]) + j * sp for j in range(len(widths))]
        ys = [y + sum(heights[:i]) + i * sp for i in range(len(heights))]
        for j in range(1, len(widths)):
            s = (xs[j] - sp, y, sp, h)
            self.fill(s, THEME["splitter"])
            self.record(f"{name}.vsplit{j}", "splitter", s, name)
        for i in range(1, len(heights)):
            s = (x, ys[i] - sp, w, sp)
            self.fill(s, THEME["splitter"])
            self.record(f"{name}.hsplit{i}", "splitter", s, name)
        for cell in node.get("cells", []):
            i, j = int(cell["row"]), int(cell["col"])
            cr = (xs[j], ys[i], widths[j], heights[i])
            cname = f"{name}.cell{i}{j}"
            self.fill(cr, THEME["panel"])
            self.record(cname, "grid_cell", cr, name, cell.get("title"))
            head = (cr[0], cr[1], cr[2], head_h)
            self.fill(head, THEME["cell_header"])
            self.fill((head[0], head[1] + head_h - 1, head[2], 1), THEME["border"])
            self.record(f"{cname}.header", "cell_header", head, cname, cell.get("title"))
            self.record(f"{cname}.header.label", "label",
                        self.text(str(cell.get("title", "")), head, THEME["title_text"]), f"{cname}.header",
                        cell.get("title"))
            for k in range(3):        # the cell menu glyph
                self.fill((head[0] + head[2] - 10, head[1] + head_h // 2 - 5 + 4 * k, 2, 2), THEME["dim"])
            if cell.get("content") is not None:
                self.draw(cell["content"], (cr[0], cr[1] + head_h, cr[2], cr[3] - head_h), cname)


def render(layout: Mapping[str, Any], size: tuple[int, int], seed: int = 0, *, text_gamma: float = 1.0) -> Screen:
    """Draw `layout` on a `size` = (width, height) canvas. Deterministic in (layout, size, seed).

    `text_gamma` raises the anti-aliased text coverage to that power before blending (1.0 is the
    reference rasterizer); another value is a font-smoothing change that touches only glyph edges.
    """
    painter = _Painter(size, seed, text_gamma)
    painter.draw(layout, (0, 0, size[0], size[1]), None)
    return Screen(painter.img, painter.out)


def node(layout: Mapping[str, Any], name: str) -> dict:
    """The node named `name` in a layout (depth first), for deriving mutated layouts."""
    stack: list[Any] = [layout]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if n.get("name") == name:
                return n
            stack.extend(n.get("children", []))
            stack.extend(c.get("content") for c in n.get("cells", []) if c.get("content") is not None)
    raise KeyError(name)


def active_trader_cell_layout(size: tuple[int, int] = (480, 360), build: str = "base",
                              symbol: str = "/ESZ26") -> dict:
    """The Active Trader ladder in one Flexible Grid cell (1.11). `build` is `base` (28 px cell
    header, viewport clamped to the space below the strip) or `candidate` (22 px header, viewport
    at least 314 px tall, measured up from its bottom)."""
    if build not in ("base", "candidate"):
        raise ValueError("build is base or candidate")
    w, h = size
    ladder = {"type": "ladder", "name": "ladder", "viewport_min_h": None if build == "base" else 314}
    return {"type": "window", "name": "cell", "children": [{
        "type": "flexgrid", "name": "grid", "rect": [0, 0, w, h], "col_widths": [w], "row_heights": [h],
        "header_h": 28 if build == "base" else 22,
        "cells": [{"row": 0, "col": 0, "title": f"Active Trader  {symbol}", "content": ladder}]}]}


def worked_example(build: str = "base", seed: int = 0) -> Screen:
    """The 480x360 Active Trader cell of the worked example (1.11), base or candidate build."""
    return render(active_trader_cell_layout((480, 360), build), (480, 360), seed)


def _price(rng: np.random.Generator, lo: float, hi: float) -> float:
    return round(float(rng.uniform(lo, hi)) * 4) / 4


def sample_layout(seed: int = 0) -> dict:
    """A 640x400 trading screen: watchlist, position statement (P/L green or red), order entry."""
    rng = np.random.default_rng(seed)
    symbols = ["AAPL", "MSFT", "NVDA", "SPY", "QQQ", "TSLA", "AMZN"]
    watch = []
    for s in symbols:
        last = _price(rng, 50, 600)
        chg = round(float(rng.uniform(-5, 5)), 2)
        watch.append([s, f"{last:.2f}", {"text": f"{chg:+.2f}", "color": "green" if chg >= 0 else "red"},
                      f"{last - 0.01:.2f}", f"{last + 0.01:.2f}"])
    positions = []
    for s in symbols[:5]:
        qty = int(rng.integers(1, 20)) * 10
        pl = round(float(rng.uniform(-900, 900)), 2)
        positions.append([s, str(qty), f"{_price(rng, 50, 600):.2f}",
                          {"text": f"{pl:+,.2f}", "color": "green" if pl >= 0 else "red"}])
    return {"type": "window", "name": "main", "title": "Main@thinkorswim [build 1998.3.0]", "children": [
        {"type": "panel", "name": "watchlist", "title": "Watchlist", "rect": [6, 6, 306, 168], "children": [
            {"type": "grid", "name": "watchlist.grid", "rect": [4, 4, 296, 140],
             "columns": ["Symbol", "Last", "Net Chng", "Bid", "Ask"], "col_widths": [56, 60, 60, 60, 60],
             "rows": watch}]},
        {"type": "panel", "name": "positions", "title": "Position Statement", "rect": [318, 6, 316, 168], "children": [
            {"type": "grid", "name": "positions.grid", "rect": [4, 4, 306, 140],
             "columns": ["Instrument", "Qty", "Trade Price", "P/L Open"], "col_widths": [76, 60, 80, 90],
             "rows": positions}]},
        {"type": "panel", "name": "ticket", "title": "Order Entry", "rect": [6, 180, 628, 190], "children": [
            {"type": "text", "name": "ticket.side.caption", "text": "Side", "color": "dim", "rect": [10, 8, 60, 16]},
            {"type": "text", "name": "ticket.qty.caption", "text": "Qty", "color": "dim", "rect": [80, 8, 60, 16]},
            {"type": "text", "name": "ticket.symbol.caption", "text": "Symbol", "color": "dim", "rect": [150, 8, 80, 16]},
            {"type": "text", "name": "ticket.price.caption", "text": "Price", "color": "dim", "rect": [240, 8, 80, 16]},
            {"type": "text", "name": "ticket.side", "text": "BUY", "color": "green", "rect": [10, 28, 60, 18]},
            {"type": "text", "name": "ticket.qty", "text": "100", "rect": [80, 28, 60, 18]},
            {"type": "text", "name": "ticket.symbol", "text": "AAPL", "rect": [150, 28, 80, 18]},
            {"type": "text", "name": "ticket.price", "text": "150.25", "rect": [240, 28, 80, 18]},
            {"type": "text", "name": "ticket.cost.caption", "text": "Cost", "color": "dim", "rect": [10, 60, 80, 16]},
            {"type": "text", "name": "ticket.cost", "text": "15,025.00", "rect": [10, 78, 120, 18]},
            {"type": "text", "name": "ticket.bp.caption", "text": "Buying Power Effect", "color": "dim",
             "rect": [150, 60, 160, 16]},
            {"type": "text", "name": "ticket.bp", "text": "-15,025.00", "rect": [150, 78, 120, 18]},
            {"type": "button", "name": "ticket.advanced", "label": "Advanced Order", "rect": [10, 130, 120, 24]},
            {"type": "button", "name": "ticket.delete", "label": "Delete", "rect": [380, 130, 90, 24]},
            {"type": "button", "name": "ticket.send", "label": "Confirm and Send", "style": "primary",
             "rect": [480, 130, 130, 24]}]}]}


def perturb(image: np.ndarray, seed: int, count: int, amplitude: int,
            rect: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """A copy of `image` with `count` distinct random pixels (inside `rect` = (x, y, w, h) when
    given) moved by a non-zero offset of at most `amplitude` per channel. Amplitude 3 stays under
    the YIQ threshold; amplitude 60 or more makes changed pixels."""
    out = image.copy()
    H, W = image.shape[:2]
    x, y, w, h = rect if rect is not None else (0, 0, W, H)
    rng = np.random.default_rng(seed)
    idx = rng.choice(w * h, size=count, replace=False)
    ys, xs = y + idx // w, x + idx % w
    delta = rng.integers(1, amplitude + 1, size=(count, 3)) * rng.choice([-1, 1], size=(count, 3))
    out[ys, xs] = np.clip(out[ys, xs].astype(np.int32) + delta, 0, 255).astype(np.uint8)
    return out
