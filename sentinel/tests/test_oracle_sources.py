"""Oracle sources (2.8.1): readers, confidence floors, the two-pass digit check, the alternate-source
rule, exports and the OCR calibration panel."""
from datetime import datetime, timedelta, timezone

import pytest

from sentinel.oracle.sources import (FLOORS, ApiUnavailable, CalibrationPanel, CalibrationSample, ExportReader,
                                     ExportSource, ExportTable, GroundTruthApi, GroundTruthReader, Reading,
                                     check_calibration, extract, literal, problem)
from sentinel.oracle.values import FORMATS

CELL = "rgn.confirm.bp-effect"


def test_ground_truth_reader_returns_text_confidence_and_second_pass():
    r = GroundTruthReader({CELL: "-15,025.00"}, confidence={CELL: 91}, colors={CELL: (230, 60, 60)},
                          crop_refs={CELL: "sha256:abc"}).read(CELL)
    assert (r.text, r.confidence, r.second_pass, r.color, r.crop_ref, r.source) == (
        "-15,025.00", 91, "-15,025.00", (230, 60, 60), "sha256:abc", "ocr")
    assert GroundTruthReader({CELL: None}).read(CELL) is None            # an unreadable cell
    assert GroundTruthReader({}).read(CELL) is None                       # a field the source does not expose
    jab = GroundTruthReader({CELL: "x"}, source="jab").read(CELL)
    assert jab.second_pass is None and jab.source == "jab"
    with pytest.raises(ValueError):
        GroundTruthReader({}, source="api")


def test_noise_is_seeded_and_lowers_confidence():
    a = GroundTruthReader({CELL: "-15,025.00"}, noise=1.0, seed=7).read(CELL)
    b = GroundTruthReader({CELL: "-15,025.00"}, noise=1.0, seed=7).read(CELL)
    assert a == b and a.text != "-15,025.00"
    assert len(a.text) == len("-15,025.00") and a.confidence == 0.0       # 7 digits x 20 points below 95
    clean = GroundTruthReader({CELL: "-15,025.00"}, noise=0.0).read(CELL)
    assert clean.text == "-15,025.00" and clean.confidence == 95.0


def test_floors_text_70_numeric_85():
    assert FLOORS["text"] == 70 and all(FLOORS[f] == 85 for f in ("money", "price", "int", "percent"))
    r = Reading(CELL, "ocr", "BUY", 75)
    assert problem(r, "text") is None
    assert problem(r, "money") == "confidence 75 below the money floor 85"
    assert problem(None, "text") == "nothing read"


def test_two_pass_digit_count_disagreement_is_unreadable():
    r = Reading(CELL, "ocr", "15,025.00", 99, second_pass="15,02.00")
    assert "disagree on digit count" in problem(r, "money")
    assert problem(Reading(CELL, "ocr", "15,025.00", 99, second_pass="15,O25.0O"), "money")    # 7 vs 5 digits
    assert problem(Reading(CELL, "ocr", "15,025.00", 99, second_pass="15,026.00"), "money") is None  # same count


def test_extract_falls_back_to_jab_then_export_and_records_the_event():
    ocr = GroundTruthReader({CELL: "-15,025.00"}, confidence=60)
    jab = GroundTruthReader({}, source="jab")
    table = ExportTable("copy_all", ("Field", "Value"), (("BP", "-15,025.00"),))
    export = ExportSource(table, ("Field", "Value"), field_map={CELL: ("Value", 0)})
    ext = extract(CELL, "money", {"ocr": ocr, "jab": jab, "export": export})
    assert ext.readable and ext.source == "export" and ext.text == "-15,025.00"
    assert [a.source for a in ext.attempts] == ["ocr", "jab", "export"]
    fb = ext.fallback
    assert (fb.from_source, fb.to_source) == ("ocr", "export") and "below the money floor" in fb.why
    assert fb.event() == f"oracle_source_fallback:{CELL}:ocr->export"
    assert "confidence 60" in ext.reads()


def test_extract_unreadable_everywhere_keeps_every_read():
    ocr = GroundTruthReader({CELL: "-15,025.00"}, confidence=60)
    jab = GroundTruthReader({CELL: "-15,025.00"}, source="jab", confidence=50)
    ext = extract(CELL, "money", {"ocr": ocr, "jab": jab})
    assert not ext.readable and ext.source is None and ext.fallback is None
    assert [a.rejected is not None for a in ext.attempts] == [True, True]
    assert ext.to_record()["readable"] is False


def test_extract_declared_jab_falls_back_to_ocr_and_exclude():
    ocr = GroundTruthReader({CELL: "-15,025.00"})
    jab = GroundTruthReader({}, source="jab")
    ext = extract(CELL, "money", {"ocr": ocr, "jab": jab}, declared="jab")
    assert ext.source == "ocr" and ext.fallback.from_source == "jab"
    assert not extract(CELL, "money", {"ocr": ocr, "jab": jab}, declared="jab", exclude=frozenset({"ocr"})).readable


def test_export_table_parse_and_header_check():
    csv_text = "Symbol,Qty,P/L Open\nAAPL,100,\"1,150.00\"\n/MESZ26,1,-76.25\n"
    t = ExportTable.parse(csv_text, "file")
    assert t.header == ("Symbol", "Qty", "P/L Open") and t.column("P/L Open") == ("1,150.00", "-76.25")
    assert t.column("Mark") is None
    ok = ExportSource(t, ("Qty", "Symbol", "P/L Open"))                 # a set: order does not matter
    bad = ExportSource(t, ("Symbol", "Qty", "P/L Day"))
    assert ok.header_matches and ok.column("Qty") == ("100", "1")
    assert not bad.header_matches and bad.column("Qty") is None
    assert isinstance(ok, ExportReader) and not isinstance(GroundTruthReader({}), ExportReader)
    tsv = ExportTable.parse("Symbol\tQty\nAAPL\t100\n", "copy_all")
    assert tsv.rows == (("AAPL", "100"),)
    none = ExportSource(None, ("Symbol",), kind="file")
    assert not none.produced and not none.header_matches and none.read("x") is None
    with pytest.raises(ValueError):
        ExportSource(None, ("Symbol",))
    with pytest.raises(ValueError):
        ExportTable("pdf", (), ())


def _panel() -> CalibrationPanel:
    known = {"money": "$12,345.67", "price": "150.25", "int": "1,000", "percent": "+1.25%", "date": "11/20/26",
             "time": "10:31:05", "text": "Confirm and Send"}
    return CalibrationPanel(tuple(CalibrationSample(f"cal.{f}", f, v) for f, v in known.items()))


def test_calibration_panel_needs_every_field_type():
    with pytest.raises(ValueError, match="lacks a sample of"):
        CalibrationPanel((CalibrationSample("cal.money", "money", "$1.00"),))
    assert {s.fmt for s in _panel().samples} == set(FORMATS)


def test_calibration_exact_read_passes_and_a_miss_is_reported():
    panel = _panel()
    truth = {s.field: s.expected for s in panel.samples}
    assert check_calibration(panel, GroundTruthReader(truth)).passed
    missed = check_calibration(panel, GroundTruthReader(truth, misread={"cal.money": "$12,345.61"}))
    assert not missed.passed and [m.field for m in missed.misses] == ["cal.money"]
    assert missed.misses[0].why == "read differs from the known value"
    low = check_calibration(panel, GroundTruthReader(truth, confidence={"cal.int": 80}))
    assert [m.field for m in low.misses] == ["cal.int"]


def test_ground_truth_api_quotes_as_of_and_fails_when_down():
    t0 = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
    api = GroundTruthApi({("AAPL", "last"): [(t0, "180.10"), (t0 + timedelta(minutes=10), "180.40")]})
    assert api.quote("AAPL", "last", t0 + timedelta(minutes=5)).text == "180.10"
    assert api.quote("AAPL", "last", t0 + timedelta(minutes=10)).text == "180.40"
    assert api.quote("AAPL", "last", t0 - timedelta(minutes=1)) is None
    with pytest.raises(ApiUnavailable):
        GroundTruthApi({}, available=False).quote("AAPL", "last", t0)


def test_literal_extraction_is_readable():
    ext = literal("rgn.x", "money", "1.00")
    assert ext.readable and ext.value() == 1 and ext.confidence == 100.0
