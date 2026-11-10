"""The Oracle's typed field formats (2.8.1) and the R10 format test (2.8.3)."""
from datetime import date, time
from decimal import Decimal

import pytest

from sentinel.oracle.values import (FORMATS, FormatError, coerce, digit_count, format_error, parse, rows, show,
                                    to_cent)


@pytest.mark.parametrize("text, value", [
    ("$15,025.00", "15025.00"), ("15,025.00", "15025.00"), ("15025.00", "15025.00"), ("-$15,025.00", "-15025.00"),
    ("$-15,025.00", "-15025.00"), ("−$12.00", "-12.00"), ("($1,234.56)", "-1234.56"), ("(1,234.56)", "-1234.56"),
    ("+1,150.00", "1150.00"), ("0.62", "0.62"), (".50", "0.50"), (" $ 1,000.00 ", "1000.00"),
])
def test_money_formats(text, value):
    assert parse("money", text) == Decimal(value)


@pytest.mark.parametrize("text, why", [
    ("NaN", "NaN"), ("Infinity", "Infinity"), ("-Infinity", "Infinity"), ("∞", "Infinity"), ("null", "null"),
    ("N/A", "N/A"), ("--", "placeholder --"), ("", "empty"), ("--5.00", "double sign"), ("+-5.00", "double sign"),
    ("(-5.00)", "double sign"), ("-(5.00)", "double sign"), ("1,50,25.00", "misplaced separator"),
    ("15,02.00", "misplaced separator"), ("1.234,56", "misplaced separator"), ("1.2.3", "misplaced separator"),
    ("($5.00", "unbalanced parentheses"), ("5.00%", "money field with a percent sign"),
])
def test_r10_reasons(text, why):
    assert format_error("money", text) == why
    with pytest.raises(FormatError):
        parse("money", text)


def test_price_with_order_type_and_at_sign():
    assert parse("price", "150.25 LMT") == Decimal("150.25")
    assert parse("price", "@5,325.00 STP LMT") == Decimal("5325.00")
    assert parse("price", "1.0825") == Decimal("1.0825")
    assert format_error("money", "150.25 LMT") == "not a money"
    assert format_error("price", "132'16") == "not a price"         # treasury 32nds are not supported


def test_int_and_percent():
    assert parse("int", "1,000") == 1000 and parse("int", "-1") == -1 and parse("int", "+100") == 100
    assert format_error("int", "100.0") == "decimal point on an integer field"
    assert format_error("int", "$100") == "int field with a currency symbol"
    assert parse("percent", "+1.25%") == Decimal("1.25") and parse("percent", "(0.56%)") == Decimal("-0.56")
    assert parse("percent", "12.5") == Decimal("12.5")


def test_dates_and_times():
    for text in ("11/20/26", "11/20/2026", "2026-11-20", "20 NOV 26", "20 Nov 2026"):
        assert parse("date", text) == date(2026, 11, 20)
    assert format_error("date", "02/30/26") == "impossible date '02/30/26'"
    assert format_error("date", "NOV 20") == "not a date"
    assert parse("time", "10:31:05 ET") == time(10, 31, 5)
    assert parse("time", "9:30") == time(9, 30)
    assert parse("time", "2:05:00 PM") == time(14, 5)
    assert format_error("time", "25:00:00") == "time out of range"


def test_text_rows_digits_and_show():
    assert parse("text", "  Confirm   and Send ") == "Confirm and Send"
    assert rows("100\n\n -1 \n") == ["100", "-1"] and rows(None) == []
    assert digit_count("$1,234.50") == 6
    assert show("money", Decimal("-15025")) == "-$15,025.00"
    assert show("int", Decimal("1000")) == "1,000" and show("percent", Decimal("1.250")) == "1.25%"
    assert show("date", date(2026, 11, 20)) == "2026-11-20"
    assert to_cent(Decimal("0.005")) == Decimal("0.01")


def test_coerce_and_unknown_format():
    assert coerce("int", 100) == Decimal(100)
    assert coerce("price", 150.25) == Decimal("150.25")
    assert coerce("text", "BUY") == "BUY"
    assert coerce("date", date(2026, 11, 20)) == date(2026, 11, 20)
    assert set(FORMATS) == {"money", "price", "int", "percent", "date", "time", "text"}
    with pytest.raises(ValueError):
        parse("hex", "ff")
