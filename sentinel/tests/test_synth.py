"""The synthetic screen generator: determinism, ground truth, and the worked example of 1.11."""
import copy

import numpy as np
import pytest

from sentinel import synth


def _rect_overlap_y(a, b):
    return max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))


def test_render_is_deterministic_and_seeded():
    a = synth.render(synth.sample_layout(3), (640, 400), seed=7)
    b = synth.render(synth.sample_layout(3), (640, 400), seed=7)
    c = synth.render(synth.sample_layout(4), (640, 400), seed=7)
    assert a.image.dtype == np.uint8 and a.image.shape == (400, 640, 3)
    assert np.array_equal(a.image, b.image) and a.components == b.components
    assert not np.array_equal(a.image, c.image)


def test_components_report_name_bbox_parent_and_text():
    s = synth.render(synth.sample_layout(0), (640, 400))
    send = s.get("ticket.send")
    assert send.kind == "button" and send.text == "Confirm and Send" and send.parent == "ticket"
    label = s.get("ticket.send.label")
    x, y, w, h = label.bbox
    bx, by, bw, bh = send.bbox
    assert bx <= x and x + w <= bx + bw and by <= y and y + h <= by + bh
    assert s.get("positions.grid.r0c3").text.startswith(("+", "-"))
    assert s.crop("ticket.send").shape == (bh, bw, 3)
    with pytest.raises(KeyError):
        s.get("nope")


def test_worked_example_base_geometry():
    s = synth.worked_example("base")
    assert s.image.shape == (360, 480, 3)
    assert s.get("grid.cell00.header").bbox == (0, 0, 480, 28)
    assert s.get("ladder.strip").bbox == (0, 28, 480, 30)
    assert s.get("ladder.viewport").bbox == (0, 58, 480, 294)
    rows = [c for c in s.components if c.kind == "price_row"]
    assert len(rows) == 14 and all(r.bbox[3] == 21 for r in rows)
    strip, vp = s.get("ladder.strip").bbox, s.get("ladder.viewport").bbox
    assert vp[1] - (strip[1] + strip[3]) == 0          # 0 px gap, touching at y=58
    assert _rect_overlap_y(strip, vp) == 0


def test_worked_example_candidate_overlaps_the_order_strip_by_14px():
    s = synth.worked_example("candidate")
    assert s.get("grid.cell00.header").bbox == (0, 0, 480, 22)
    assert s.get("ladder.strip").bbox == (0, 22, 480, 30)
    assert s.get("ladder.viewport").bbox == (0, 38, 480, 314)
    assert _rect_overlap_y(s.get("ladder.strip").bbox, s.get("ladder.viewport").bbox) == 14
    # the first price row draws over the lower half of BUY MKT and SELL MKT
    row0 = s.get("ladder.row0").bbox
    for button in ("ladder.buy", "ladder.sell"):
        b = s.get(button).bbox
        assert row0[1] <= b[1] + b[3] // 2 and _rect_overlap_y(row0, b) >= b[3] // 2
    # and the pixels agree: the lower half of BUY MKT no longer shows the button color
    bx, by, bw, bh = s.get("ladder.buy").bbox
    lower = s.image[by + bh - 4, bx + 2:bx + bw - 2]
    assert not (lower == np.array(synth.THEME["buy"])).all(axis=-1).any()


def test_candidate_at_taller_cells_has_no_overlap_but_moves_6px():
    for h in (480, 720):
        b = synth.render(synth.active_trader_cell_layout((640, h), "base"), (640, h))
        c = synth.render(synth.active_trader_cell_layout((640, h), "candidate"), (640, h))
        assert _rect_overlap_y(c.get("ladder.strip").bbox, c.get("ladder.viewport").bbox) == 0
        assert b.get("ladder.viewport").bbox[1] - c.get("ladder.viewport").bbox[1] == 6


def test_flexible_grid_cells_resize():
    content = {"type": "rect", "name": "plot", "color": "bg"}
    layout = {"type": "window", "children": [{
        "type": "flexgrid", "name": "fg", "col_widths": [300, 336], "row_heights": [200, 196],
        "cells": [{"row": r, "col": c, "title": f"Chart {r}{c}", "content": dict(content, name=f"plot{r}{c}")}
                  for r in range(2) for c in range(2)]}]}
    s = synth.render(layout, (640, 400))
    assert s.get("fg.cell00").bbox == (0, 0, 300, 200)
    assert s.get("fg.cell11").bbox == (304, 204, 336, 196)
    assert s.get("fg.vsplit1").bbox == (300, 0, 4, 400)
    assert s.get("plot11").bbox == (304, 232, 336, 168)       # below the 28 px cell header
    wider = copy.deepcopy(layout)
    synth.node(wider, "fg")["col_widths"] = [360, 276]
    assert synth.render(wider, (640, 400)).get("fg.cell01").bbox == (364, 0, 276, 200)


def test_text_gamma_changes_only_glyph_edges():
    a = synth.render(synth.sample_layout(0), (640, 400)).image
    b = synth.render(synth.sample_layout(0), (640, 400), text_gamma=2.0).image
    diff = (a != b).any(axis=-1)
    assert diff.any()
    # flat areas such as the Confirm and Send button body are untouched
    assert not diff[352:356, 490:500].any()


def test_perturb_changes_exactly_count_pixels_inside_rect():
    img = synth.render(synth.sample_layout(0), (640, 400)).image
    out = synth.perturb(img, seed=5, count=10, amplitude=3, rect=(100, 100, 50, 40))
    changed = (out != img).any(axis=-1)
    ys, xs = np.nonzero(changed)
    assert changed.sum() <= 10 and changed.sum() >= 8          # clipping at 0/255 can cancel a change
    assert ys.min() >= 100 and ys.max() < 140 and xs.min() >= 100 and xs.max() < 150
    assert np.abs(out.astype(int) - img.astype(int)).max() <= 3
    assert np.array_equal(out, synth.perturb(img, seed=5, count=10, amplitude=3, rect=(100, 100, 50, 40)))


def test_node_finds_named_nodes_and_rejects_bad_build():
    layout = synth.sample_layout(0)
    assert synth.node(layout, "ticket.price")["text"] == "150.25"
    assert synth.node(synth.active_trader_cell_layout(), "ladder")["type"] == "ladder"
    with pytest.raises(KeyError):
        synth.node(layout, "nope")
    with pytest.raises(ValueError):
        synth.active_trader_cell_layout(build="other")
