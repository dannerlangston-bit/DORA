"""Tests for sentinel.oracle.tesseract: preprocessing, command lines, TSV parsing and confidence mapping on recorded
Tesseract output and synthetic crops; one test reads rendered digits through the real program when it is installed."""
import shutil
from decimal import Decimal

import cv2
import numpy as np
import pytest

from sentinel.capture.redaction import OcrWord
from sentinel.diff.regions import Rect
from sentinel.oracle import tesseract as ts
from sentinel.oracle.sources import CalibrationPanel, CalibrationSample, ValueSource, check_calibration, extract
from sentinel.oracle.values import FORMATS

HEAD = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def tsv(*lines):
    """Tesseract 5 TSV: the page, block, paragraph and line rows (conf -1, empty text), then word rows."""
    rows = [HEAD + "1\t1\t0\t0\t0\t0\t0\t0\t360\t60\t-1\t"]
    for (block, par, line), words in lines:
        rows.append(f"4\t1\t{block}\t{par}\t{line}\t0\t12\t9\t330\t42\t-1\t")
        for i, (text, conf, left) in enumerate(words, 1):
            rows.append(f"5\t1\t{block}\t{par}\t{line}\t{i}\t{left}\t9\t{20 * len(text)}\t42\t{conf}\t{text}")
    return "\n".join(rows) + "\n"


# Recorded: "$15,025.00" on a dark money cell at 3x and 2x (Tesseract 5.3, LSTM).
MONEY_3X = tsv(((1, 1, 1), [("$15,025.00", 96.410179, 12)]))
MONEY_2X = tsv(((1, 1, 1), [("$15,025.00", 93.024361, 8)]))
# Recorded: a two-row region, one word uncertain.
ROWS_3X = tsv(((1, 1, 1), [("SPY", 95.1, 4), ("100", 91.5, 90)]), ((1, 1, 2), [("QQQ", 63.25, 4), ("-50", 90.0, 90)]))


class Runner:
    """The program, answering each pass by its --dpi with recorded TSV."""

    def __init__(self, by_dpi, code=0):
        self.by_dpi, self.code = by_dpi, code
        self.calls = []

    def __call__(self, args, stdin, timeout):
        self.calls.append((args, stdin))
        if "--version" in args:
            return 0, "tesseract 5.3.0\n leptonica-1.82.0\n", ""
        out = self.by_dpi.get(args[args.index("--dpi") + 1])
        if out is None:
            return 1, "", "Error during processing."
        return self.code, out, ""

    def image(self, i):
        return cv2.imdecode(np.frombuffer(self.calls[i][1], np.uint8), cv2.IMREAD_UNCHANGED)


def dark_cell(w=40, h=12):
    img = np.full((h, w, 3), (18, 18, 18), np.uint8)
    img[3:9, 5:35] = (92, 196, 24)                       # green text pixels (BGR)
    return img


def ocr(crops, runner):
    return ts.TesseractOcr(crops.get, run=runner)


def test_it_is_an_ocr_reader_and_a_word_reader():
    o = ocr({}, Runner({}))
    assert isinstance(o, ValueSource) and o.name == "ocr" and callable(o.words)


def test_parse_keeps_word_rows_with_confidence_and_text():
    words = ts.parse_tsv(ROWS_3X + "5\t1\t1\t1\t3\t1\t0\t0\t9\t9\t-1\t\n5\t1\t1\t1\t3\t2\t0\t0\t9\t9\t88\t   \n"
                                   "5\tbroken\n")
    assert [(w.text, w.conf, w.line) for w in words] == [("SPY", 95.1, (1, 1, 1)), ("100", 91.5, (1, 1, 1)),
                                                         ("QQQ", 63.25, (1, 1, 2)), ("-50", 90.0, (1, 1, 2))]
    assert (words[1].left, words[1].width) == (90, 60)
    tess4 = HEAD + "5\t1\t1\t1\t1\t1\t3\t4\t50\t20\t87\t1,234\n"            # Tesseract 4: integer confidence
    assert ts.parse_tsv(tess4)[0].conf == 87.0


def test_lines_join_and_the_lowest_word_confidence_wins():
    assert ts.text_and_confidence(ts.parse_tsv(ROWS_3X)) == ("SPY 100\nQQQ -50", 63.25)
    assert ts.text_and_confidence([]) == ("", 0.0)                           # choice 1: nothing read is unreadable
    over = ts.parse_tsv(HEAD + "5\t1\t1\t1\t1\t1\t0\t0\t9\t9\t100.4\tx\n")
    assert ts.text_and_confidence(over)[1] == 100.0


def test_prepare_inverts_dark_crops_and_upscales():
    dark = dark_cell()
    up = ts.prepare(dark, 3)
    assert up.shape == (36, 120) and up.dtype == np.uint8
    assert up[0, 0] > 200 and up[18, 60] < up[0, 0]                          # dark ground became light
    light = 255 - dark
    assert ts.prepare(light, 2)[0, 0] > 200 and ts.prepare(light, 2).shape == (24, 80)
    assert ts.prepare(light, 2, dark=True)[0, 0] < 60                        # an explicit theme wins


def test_text_color_is_the_rgb_of_the_text_pixels():
    assert ts.text_color(dark_cell()) == (24, 196, 92)
    assert ts.text_color(cv2.cvtColor(dark_cell(), cv2.COLOR_BGR2GRAY)) is None
    assert ts.text_color(np.zeros((5, 5, 3), np.uint8)) is None


def test_whitelists_cover_every_format_and_money_has_no_letters():
    assert set(ts.WHITELISTS) == set(FORMATS)
    assert not any(c.isalpha() for c in ts.WHITELISTS["money"])
    assert set("$,.-()") <= set(ts.WHITELISTS["money"]) and "%" in ts.WHITELISTS["percent"]
    assert set("LMTSPKRGOC") <= set(ts.WHITELISTS["price"]) and "@" in ts.WHITELISTS["price"]
    assert set("NOVDEC/") <= set(ts.WHITELISTS["date"]) and set("APMEST:") <= set(ts.WHITELISTS["time"])
    assert ts.WHITELISTS["text"] is None


def test_command_lines_per_pass():
    o = ocr({}, Runner({}))
    money = o.command(psm=7, scale=3, fmt="money")
    assert money[:3] == [o.binary, "stdin", "stdout"] and money[-1] == "tsv"
    assert money[money.index("--dpi") + 1] == "288" and money[money.index("--psm") + 1] == "7"
    assert f"tessedit_char_whitelist={ts.WHITELISTS['money']}" in money and "load_system_dawg=0" in money
    text = o.command(psm=6, scale=2, fmt="text")
    assert not any(a.startswith("tessedit_char_whitelist") for a in text) and "192" in text


def test_a_money_read_is_both_passes_with_confidence_crop_and_color():
    runner = Runner({"288": MONEY_3X, "192": MONEY_2X})
    o = ocr({"acct.net_liq": ts.OcrCrop(dark_cell(), "money", ref="sha256:crop")}, runner)
    r = o.read("acct.net_liq")
    assert (r.source, r.text, r.second_pass, r.crop_ref, r.color) == ("ocr", "$15,025.00", "$15,025.00",
                                                                      "sha256:crop", (24, 196, 92))
    assert r.confidence == pytest.approx(96.410179)
    first, second = runner.image(0), runner.image(1)
    assert first.shape == (36, 120) and second.shape == (24, 80) and first[0, 0] > 200    # 3x then 2x, inverted
    assert extract("acct.net_liq", "money", {"ocr": o}).value() == Decimal("15025.00")


def test_multi_row_regions_use_block_segmentation():
    runner = Runner({"288": ROWS_3X, "192": ROWS_3X})
    o = ocr({"pos": ts.OcrCrop(dark_cell(), "text", single_line=False)}, runner)
    r = o.read("pos")
    assert r.text == "SPY 100\nQQQ -50" and r.confidence == 63.25
    assert runner.calls[0][0][runner.calls[0][0].index("--psm") + 1] == "6"


def test_engine_failures_read_nothing_or_leave_the_second_pass_out():
    crops = {"f": ts.OcrCrop(dark_cell(), "money")}
    assert ocr(crops, Runner({"192": MONEY_2X})).read("f") is None           # 3x pass failed
    one = ocr(crops, Runner({"288": MONEY_3X})).read("f")                    # 2x pass failed
    assert one.text == "$15,025.00" and one.second_pass is None
    assert extract("f", "money", {"ocr": ocr(crops, Runner({"288": MONEY_3X}))}).readable is False
    assert ocr(crops, Runner({})).read("missing") is None
    with pytest.raises(ValueError, match="unknown format"):
        ocr({"g": ts.OcrCrop(dark_cell(), "currency")}, Runner({"288": MONEY_3X})).read("g")


def test_an_empty_crop_is_unreadable_not_a_pass():
    crops = {"f": ts.OcrCrop(np.full((12, 40, 3), 18, np.uint8), "money")}
    o = ocr(crops, Runner({"288": tsv(), "192": tsv()}))
    r = o.read("f")
    assert (r.text, r.confidence) == ("", 0.0) and not extract("f", "money", {"ocr": o}).readable


class Panel:
    """The calibration panel's crops, and the program reading each one as `texts` says."""

    def __init__(self, texts):
        self.texts, self.current = texts, None

    def crop(self, field):
        self.current = self.texts[field]
        return ts.OcrCrop(dark_cell(), field.split(".")[1])

    def run(self, args, stdin, timeout):
        words = self.current.split()
        return 0, tsv(((1, 1, 1), [(w, 97.0, 4 + 60 * i) for i, w in enumerate(words)])), ""


def test_the_calibration_panel_passes_on_exact_reads_and_misses_otherwise():
    texts = {"cal.money": "$1,234.56", "cal.price": "512.25", "cal.int": "1,000", "cal.percent": "+1.25%",
             "cal.date": "11/10/26", "cal.time": "9:30:00", "cal.text": "Net Liq"}
    panel = CalibrationPanel(tuple(CalibrationSample(f, f.split(".")[1], t) for f, t in texts.items()))
    reads = Panel(dict(texts))
    o = ts.TesseractOcr(reads.crop, run=reads.run)
    assert check_calibration(panel, o).passed
    reads.texts["cal.money"] = "$1,284.56"
    assert [m.field for m in check_calibration(panel, o).misses] == ["cal.money"]


def test_words_are_boxes_in_the_given_image():
    runner = Runner({"288": ROWS_3X})
    o = ocr({}, runner)
    words = o.words(np.full((60, 360), 255, np.uint8))
    assert words[0] == OcrWord("SPY", Rect(4, 9, 60, 42), 95.1) and len(words) == 4
    assert runner.calls[0][0][runner.calls[0][0].index("--psm") + 1] == "11"
    assert runner.image(0).shape == (60, 360)                                # no second upscale
    with pytest.raises(ts.OcrEngineError):
        ocr({}, Runner({})).words(np.zeros((5, 5), np.uint8))


def test_version_and_a_missing_program():
    assert ocr({}, Runner({})).version() == "5.3.0"
    assert ts.parse_version("tesseract v5.0.0-alpha-20201231\n") == "5.0.0-alpha-20201231"
    assert ts.parse_version("something else") is None
    with pytest.raises(ts.TesseractUnavailable):
        ts.TesseractOcr(binary="/nonexistent/tesseract-sentinel")


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_synth_rendered_digits_read_exactly_through_the_real_program():
    img = np.full((28, 160, 3), 18, np.uint8)
    cv2.putText(img, "15,025.00", (6, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (220, 220, 220), 1, cv2.LINE_AA)
    o = ts.TesseractOcr({"f": ts.OcrCrop(img, "money")}.get)
    r = o.read("f")
    assert r.text == "15,025.00" and r.confidence >= 85 and r.second_pass == r.text
