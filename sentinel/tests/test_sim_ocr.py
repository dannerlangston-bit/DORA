"""The pixel-true OCR of the synthetic screens (sim_ocr; 2.7 Bounds Branch B, 2.8.1, 10.3 canary mutants)."""
import cv2
import numpy as np
import pytest

from sentinel import canary as cn
from sentinel import sim_ocr, synth
from sentinel.pipeline.sim import _ink_box, _words

BG, TEXT = (26, 27, 31), (198, 200, 204)


def line(text: str, *, scale: float = 0.36, line_type: int = cv2.LINE_AA, org=(10, 25), size=(240, 40),
         color=TEXT, bg=BG) -> np.ndarray:
    """One line of text blended on a flat background, the way synth draws it."""
    img = np.empty((size[1], size[0], 3), np.uint8)
    img[:] = bg
    alpha = np.zeros((size[1], size[0]), np.uint8)
    cv2.putText(alpha, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, 255, 1, line_type)
    a = alpha.astype(np.float32)[..., None] / 255.0
    return np.rint(img * (1 - a) + np.array(color, np.float32) * a).astype(np.uint8)


def texts(words):
    return [w.text for w in words]


def test_every_word_synth_draws_reads_back_exactly_with_its_box():
    screen = synth.render(synth.sample_layout(2), (640, 400), seed=7)
    want = []
    for d in screen.components:
        if d.kind in ("label", "text", "cell", "header_cell") and d.text:
            ink = _ink_box(d)
            want += [(t, b) for t, b in _words(d, ink)] if ink else []
    got = sim_ocr.read_words(screen.image)
    assert sorted(texts(got)) == sorted(t for t, _ in want)
    assert all(w.confidence >= sim_ocr.WORD_CONFIDENCE for w in got)
    first = {t: b for t, b in reversed(want)}                  # the first word of each drawn text: same box
    for w in got:
        if w.text == "Confirm":
            assert w.bbox == first["Confirm"] and w.cap_height == 10.0


def test_a_clipped_label_reads_as_its_visible_prefix_and_a_blank_reads_nothing():
    img = line("Trade Price")
    clipped = img.copy()
    clipped[:, 58:] = BG                                       # "Trade Price" cut inside "Price"
    assert texts(sim_ocr.read_words(img)) == ["Trade", "Price"]
    got = texts(sim_ocr.read_words(clipped))
    assert got[0] == "Trade" and "Price".startswith(got[1]) and got[1] != "Price"
    blank = img.copy()
    blank[:] = BG
    assert sim_ocr.read_words(blank) == []


def test_a_transplanted_digit_reads_as_the_glyph_now_there():
    img = line("-15,025.00   7", size=(200, 40))
    box = (8, 15, 70, 14)                                      # the value only; the donor 7 sits in the same band
    out = cn.replace_glyph(img, box, np.random.default_rng(3), position="first")
    value = texts(sim_ocr.read_words(out, rects=[box]))[0]
    assert value != "-15,025.00" and len(value) == len("-15,025.00") and value.replace(",", "").replace(".", "") \
        .lstrip("-").isdigit()


def test_text_a_mutant_pastes_at_another_size_reads_with_the_same_face():
    field = np.empty((120, 90, 3), np.uint8)
    field[:] = BG
    loading = cn.paste_text(field, (0, 0, 90, 120), np.random.default_rng(0))   # canary M15: fitted scale, 8-connected
    words = sim_ocr.read_words(loading)
    assert texts(words) == ["Loading..."] and words[0].cap_height > 10
    typed = cn.type_value(line("100", size=(80, 30), org=(8, 20)), (2, 5, 60, 20), np.random.default_rng(0))
    assert texts(sim_ocr.read_words(typed)) == ["10"]


def test_a_row_shrunk_to_a_5_px_cap_height_reads_small():
    img = line("1.0825 6,210.50", size=(200, 40))
    out = cn.shrink_row(img, (5, 12, 190, 18), np.random.default_rng(0), cap_height=5)
    words = sim_ocr.read_words(out)
    assert words and all(w.cap_height < 7 and w.confidence >= 100 * sim_ocr.SMALL_QUALITY for w in words)
    assert all(w.bbox[3] < 9 for w in words)


def test_three_dots_after_a_clipped_word_read_as_an_ellipsis():
    img = line("Ask Size")
    out = cn.clip_label(img, (8, 14, 44, 14), np.random.default_rng(0), fraction=0.25, style="ellipsis")
    got = texts(sim_ocr.read_words(out))
    assert got[0] == "Ask" and got[1].endswith("...") and "Size".startswith(got[1][:-3])


def test_graphics_and_lines_never_read_as_words():
    img = np.empty((120, 300, 3), np.uint8)
    img[:] = BG
    img[20:60, 30:36] = (24, 196, 92)                          # a candle
    img[40:42, 10:290] = (255, 200, 0)                         # a study line across it
    img[80:81, :] = (58, 60, 66)                               # a grid line
    cv2.rectangle(img, (200, 90), (209, 99), (130, 132, 138), 1)   # a checkbox
    mirrored = 255 - line("Confirm and Send", color=(236, 236, 236), bg=(30, 86, 160))[:, ::-1]
    assert sim_ocr.read_words(img) == []
    assert sim_ocr.read_words(np.ascontiguousarray(mirrored)) == []


def test_a_rect_reads_the_lines_that_touch_it_whole():
    img = line("BUY 100 AAPL", size=(200, 40))
    assert texts(sim_ocr.read_words(img, rects=[(40, 18, 4, 4)])) == ["BUY", "100", "AAPL"]
    assert sim_ocr.read_words(img, rects=[(150, 0, 40, 10)]) == []


def test_read_words_takes_an_rgb_frame():
    with pytest.raises(ValueError):
        sim_ocr.read_words(np.zeros((10, 10), np.uint8))
