"""Typed field values as thinkorswim displays them (2.8.1 typed fields, 2.8.3 R10).

`parse(fmt, text)` turns one displayed cell into a typed value or raises `FormatError` naming
what is wrong with it; `format_error(fmt, text)` is the R10 test (the same parser, as a reason or
None); `show(fmt, value)` writes a value back in a canonical display form for `detail` and
`raw_values`; `rows(text)` splits a multi-row region read into its rows.

Formats (`FORMATS`): the `format` enum of 4.7 (`money`, `price`, `int`, `percent`, `time`,
`text`) plus `date`, the typed field 2.8.1 lists for OCR and R07 reads.

Display conventions assumed (2.8.1 leaves them to the implementation; checked against the
example config and `sentinel.synth` screens, and re-checked against the real app in Phase 0):

- Numbers use `,` as the thousands separator in groups of exactly three and `.` as the decimal
  point, or no separator at all (`15025.00` and `15,025.00` are both valid; `1,50,25.00`,
  `15,02.00` and `1.234,56` are a misplaced separator).
- A negative number is a leading minus (`-`, or the typographic minus U+2212) or parentheses:
  `-$15,025.00`, `$-15,025.00`, `($15,025.00)`, `(15,025.00)`. A leading `+` marks a positive
  change (`+1,150.00`). Two signs, or a sign inside parentheses, is a double sign.
- `money` and `price` may carry a leading `$`; `percent` may carry a trailing `%` (the value is in
  percent units: `+1.25%` is 1.25); `int` has no decimal point.
- `price` may carry the Order Confirmation dialog's leading `@` and a trailing order-type token
  (`150.25 LMT`, `@5,325.00 STP LMT`; the tokens of `ORDER_TYPE_SUFFIXES`). Treasury 32nds
  (`132'16`) and abbreviated magnitudes (`1.2M`, `45K`) are not supported and fail to parse.
- `date` is `MM/DD/YY`, `MM/DD/YYYY`, `YYYY-MM-DD` or the option chain's `DD MMM YY`
  (`20 NOV 26`); two-digit years are 20YY.
- `time` is `H:MM` or `H:MM:SS` with an optional `AM`/`PM` and an optional trailing zone label
  (`ET`, `EST`, `EDT`), as the Order Confirmation timestamp and the Position Statement clock show.
- `text` is compared after collapsing runs of whitespace to one space and trimming.
- `NaN`, `Infinity` (and `∞`), `null`, `N/A`, `--` and an empty cell are never a number (R10).

Numbers are `Decimal`, so money and price comparisons are exact; rounding to the cent is explicit.
"""
from __future__ import annotations

import re
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: Typed field formats: the 4.7 `format` enum plus `date` (2.8.1).
FORMATS: tuple[str, ...] = ("money", "price", "int", "percent", "date", "time", "text")
NUMERIC_FORMATS = frozenset({"money", "price", "int", "percent"})
ORDER_TYPE_SUFFIXES: tuple[str, ...] = ("STP LMT", "TRG LMT", "LMT", "MKT", "STP", "TRSTP", "MOC", "LOC", "TRG")
CENT = Decimal("0.01")

Value = Decimal | date | time | str

_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+"
_NUMBER = re.compile(
    rf"^(?P<open>\()?(?P<s1>[+\-−])?(?P<cur>\$)?(?P<s2>[+\-−])?(?P<num>{_NUM})(?P<pct>%)?(?P<close>\))?$")
_LOOSE_DIGITS = re.compile(r"^[(+\-−$]*[\d.,]+[%)]*$")
_PLACEHOLDERS = {"nan": "NaN", "infinity": "Infinity", "inf": "Infinity", "∞": "Infinity", "null": "null",
                 "none": "null", "n/a": "N/A", "na": "N/A", "--": "placeholder --", "-": "placeholder -",
                 "—": "placeholder —"}
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                                       "NOV", "DEC"), start=1)}
_DATE_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$")
_DATE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATE_CHAIN = re.compile(r"^(\d{1,2}) ([A-Za-z]{3}) (\d{2}|\d{4})$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?(?: ?([AaPp][Mm]))?(?: (?:ET|EST|EDT))?$")


class FormatError(ValueError):
    """A displayed value is not a valid value of its field type (the R10 failure reasons)."""


def normalize_text(text: str) -> str:
    """Whitespace collapsed to single spaces and trimmed (the `text` comparison form)."""
    return " ".join(text.split())


def rows(text: str | None) -> list[str]:
    """The non-empty rows of a region read (rows are separated by newlines), each trimmed."""
    if text is None:
        return []
    return [r.strip() for r in text.split("\n") if r.strip()]


def digit_count(text: str | None) -> int:
    """Number of ASCII digits in a read (the two-pass check of 2.8.1 compares this)."""
    return sum(c.isdigit() for c in text or "")


def _number(fmt: str, text: str) -> Decimal:
    t = normalize_text(text)
    if fmt == "price":
        t = t.removeprefix("@").strip()
        for suffix in ORDER_TYPE_SUFFIXES:
            if t.endswith(" " + suffix):
                t = t[: -len(suffix) - 1].strip()
                break
    t = t.replace(" ", "")
    if not t:
        raise FormatError("empty")
    key = t.lower()
    placeholder = _PLACEHOLDERS.get(key) or _PLACEHOLDERS.get(key.lstrip("+-−"))
    if placeholder:
        raise FormatError(placeholder)
    m = _NUMBER.match(t)
    if not m:
        if _LOOSE_DIGITS.match(t):
            signs = sum(t.count(c) for c in "+-−") + (1 if "(" in t else 0)
            if signs > 1:
                raise FormatError("double sign")
            if "," in t or t.count(".") > 1:
                raise FormatError("misplaced separator")
        raise FormatError(f"not a {fmt}")
    if bool(m["open"]) != bool(m["close"]):
        raise FormatError("unbalanced parentheses")
    if (m["s1"] and m["s2"]) or (m["open"] and (m["s1"] or m["s2"])):
        raise FormatError("double sign")
    if m["cur"] and fmt not in ("money", "price"):
        raise FormatError(f"{fmt} field with a currency symbol")
    if m["pct"] and fmt != "percent":
        raise FormatError(f"{fmt} field with a percent sign")
    num = m["num"]
    if fmt == "int" and "." in num:
        raise FormatError("decimal point on an integer field")
    try:
        value = Decimal(num.replace(",", ""))
    except InvalidOperation as exc:                 # pragma: no cover - the regex admits only digits
        raise FormatError(f"not a {fmt}") from exc
    negative = bool(m["open"]) or (m["s1"] or m["s2"] or "") in ("-", "−")
    return -value if negative else value


def _date(text: str) -> date:
    t = normalize_text(text)
    try:
        if m := _DATE_SLASH.match(t):
            mo, d, y = int(m[1]), int(m[2]), int(m[3])
            return date(y + 2000 if y < 100 else y, mo, d)
        if m := _DATE_ISO.match(t):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _DATE_CHAIN.match(t):
            mon = _MONTHS.get(m[2].upper())
            if mon is not None:
                y = int(m[3])
                return date(y + 2000 if y < 100 else y, mon, int(m[1]))
    except ValueError as exc:
        raise FormatError(f"impossible date {t!r}") from exc
    raise FormatError("not a date")


def _time(text: str) -> time:
    m = _TIME.match(normalize_text(text))
    if not m:
        raise FormatError("not a time")
    h, mi, s = int(m[1]), int(m[2]), int(m[3] or 0)
    if m[4]:
        if not 1 <= h <= 12:
            raise FormatError("hour out of range")
        h = h % 12 + (12 if m[4].upper() == "PM" else 0)
    try:
        return time(h, mi, s)
    except ValueError as exc:
        raise FormatError("time out of range") from exc


def parse(fmt: str, text: str | None) -> Value:
    """The typed value of one displayed cell, or `FormatError` with the R10 reason."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown field format {fmt!r}; one of {FORMATS}")
    if text is None:
        raise FormatError("nothing read")
    if fmt in NUMERIC_FORMATS:
        return _number(fmt, text)
    if fmt == "date":
        return _date(text)
    if fmt == "time":
        return _time(text)
    return normalize_text(text)


def format_error(fmt: str, text: str | None) -> str | None:
    """R10: why `text` is not a valid `fmt` value (NaN, Infinity, null, double sign, misplaced
    separator, ...), or None when it is."""
    try:
        parse(fmt, text)
    except FormatError as exc:
        return str(exc)
    return None


def coerce(fmt: str, literal: object) -> Value:
    """An expected literal from a flow or the fixture as a typed value (4.7 `expected`, `value_ref`)."""
    if fmt in NUMERIC_FORMATS and isinstance(literal, (int, float, Decimal)) and not isinstance(literal, bool):
        return Decimal(str(literal))
    if fmt == "date" and isinstance(literal, date):
        return literal
    return parse(fmt, str(literal))


def to_cent(value: Decimal) -> Decimal:
    """Rounded half up to the cent (2.8.2: money is compared exactly after rounding to the cent)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def show(fmt: str, value: Value) -> str:
    """A typed value in canonical display form: `-$15,025.00`, `150.25`, `100`, `1.25%`, ISO dates."""
    if isinstance(value, Decimal):
        if fmt == "money":
            v = to_cent(value)
            return f"{'-' if v < 0 else ''}${abs(v):,.2f}"
        if fmt == "int":
            return f"{value:,.0f}"
        if fmt == "percent":
            return f"{value.normalize():f}%"
        return f"{value:,f}"
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value)
