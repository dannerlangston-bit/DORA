"""Oracle value sources (2.8.1, 5.10): what each source read, how sure it was, and which read the
Oracle accepts.

The real readers (Tesseract or PaddleOCR on 3x upscaled inverted crops, the Java Access Bridge,
the export file and the clipboard, the market-data API) exist only on the Windows rig, so the
Oracle sees them through four interfaces that return a `Reading` (the raw text, a confidence in
0 to 100, and for OCR the second pass at 2x and the dominant text color):

- `OcrReader`, `JabReader`: `read(field) -> Reading | None` for a region id.
- `ExportReader`: an export file or Copy All text (`kind` `file` or `copy_all`) with its
  `header`, the `declared_header` it must match, `column(name)` and `read(field)` for the region
  ids it can supply. `ExportSource` implements it over an `ExportTable`.
- `ApiReader`: `quote(symbol, field, as_of) -> Reading | None`, raising `ApiUnavailable`.

`GroundTruthReader` reads a dict of field to text (the simulated pipeline feeds it the text
`sentinel.synth` drew) with injected confidence, misreads, a second OCR pass, seeded digit noise
and unreadable cells; `GroundTruthApi` answers quotes from a time series.

`extract(field, fmt, sources, declared)` is the acceptance rule of 2.8.1: a read is unreadable
below its confidence floor (`FLOORS`: 70 for text, 85 for money, price, integer and percent) or
when the two OCR passes disagree on digit count; the Oracle then tries the alternate sources in
the order of 5.10 (JAB, then grid copy or export, then OCR when the declared source was not OCR),
and records each fallback (`oracle_source_fallback`, 2.8.1). An `Extraction` keeps every read it
tried, so the reporter can show both reads (5.10).

`CalibrationPanel` and `check_calibration` are the OCR calibration panel of 2.8.1 (preflight H9,
the `canary_flow_passed` gate): checked-in crops with known values, at least one per field type,
read exactly.

Choices where the spec is ambiguous (reported):

- Floors for `date` and `time` (2.8.1 names text and the four numeric types only): 85, because a
  misread digit changes the value as it does in a number.
- An empty read (`text == ""`) is a read of an empty cell, subject to the floor like any read; a
  reader returning None means it could not read the field at all.
- The alternates a checkpoint declares are the sources the caller passes for it; the order is
  `FALLBACK_ORDER` with the declared source first. The market-data API and the fixture are never
  alternates: they are independent values, not other reads of the same cell.
- An export whose header set differs from the declared one: a `file` export makes the checkpoint
  `harness_error (reason: export_header_mismatch)` (the caller acts on `header_matches`); a
  `copy_all` text is skipped as a source, so the Oracle falls back to OCR with
  `oracle_source_fallback` (2.8.1 Grid copy).
- Calibration: a sample is missed when it reads below its floor, when its passes disagree on digit
  count, or when its text differs from the known value after trimming (H9: "exact match").
"""
from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from .values import FORMATS, Value, digit_count, parse, rows

#: 4.7 assertion `source` enum.
SOURCES: tuple[str, ...] = ("ocr", "export", "jab", "api")
#: Sources that read a region's cell; the alternates of 2.8.1 in the order of 5.10.
FALLBACK_ORDER: tuple[str, ...] = ("jab", "export", "ocr")
#: Confidence floors (2.8.1, 5.10); date and time are a choice (module docstring).
FLOORS: dict[str, float] = {"text": 70.0, "money": 85.0, "price": 85.0, "int": 85.0, "percent": 85.0,
                            "date": 85.0, "time": 85.0}
EXACT_CONFIDENCE = 100.0                       # JAB values, export cells and API values are not OCR guesses


@dataclass(frozen=True)
class Reading:
    """One source's read of one field: raw text (rows separated by newlines), confidence 0 to 100,
    and for OCR the 2x second pass, the crop reference and the dominant text color."""

    field: str
    source: str
    text: str | None
    confidence: float
    second_pass: str | None = None
    crop_ref: str | None = None
    color: tuple[int, int, int] | None = None

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"source {self.source!r} is not one of {SOURCES}")
        if not 0.0 <= self.confidence <= 100.0:
            raise ValueError(f"confidence {self.confidence} outside 0 to 100")

    def describe(self) -> str:
        return f"{self.text!r} (confidence {self.confidence:.0f})"


@runtime_checkable
class ValueSource(Protocol):
    """A source that reads a region's cell by field name."""

    name: str

    def read(self, field: str) -> Reading | None: ...


class OcrReader(ValueSource, Protocol):
    """OCR on 3x upscaled, inverted crops with a per-field whitelist; `second_pass` is the 2x read."""


class JabReader(ValueSource, Protocol):
    """Accessible value and text of cells (DR-01 Branch A); None for a cell the tree no longer exposes."""


@runtime_checkable
class ExportReader(ValueSource, Protocol):
    """An export file or Copy All text of a grid."""

    kind: str                                    # file | copy_all
    declared_header: tuple[str, ...]

    @property
    def produced(self) -> bool: ...

    @property
    def header_matches(self) -> bool: ...

    def column(self, name: str) -> tuple[str, ...] | None: ...


class ApiUnavailable(RuntimeError):
    """The market-data API did not answer (preflight N3, or a mid-run failure, 2.8.1)."""


@runtime_checkable
class ApiReader(Protocol):
    """Quote, chain and price history values as of a moment (2.8.1 Market-data API)."""

    def quote(self, symbol: str, field: str, as_of: datetime) -> Reading | None: ...


# --------------------------------------------------------------------------------------------
# Ground truth (simulated sources)


class GroundTruthReader:
    """A `ValueSource` over known values, with injected imperfections.

    `values` maps field to the true text (None: the cell cannot be read at all). `confidence` is a
    default or per-field confidence; `misread` replaces the text the reader returns (a wrong read,
    or a source that disagrees with another); `second_pass` is the 2x OCR read (default: the same
    text); `noise` is the per-digit probability of a substituted digit, seeded by `seed` and the
    field name, each substitution lowering confidence by `noise_penalty`; `colors` and `crop_refs`
    are passed through.
    """

    def __init__(self, values: Mapping[str, str | None], *, source: str = "ocr",
                 confidence: float | Mapping[str, float] = 95.0, misread: Mapping[str, str] | None = None,
                 second_pass: Mapping[str, str] | None = None, noise: float = 0.0, seed: int = 0,
                 noise_penalty: float = 20.0, colors: Mapping[str, tuple[int, int, int]] | None = None,
                 crop_refs: Mapping[str, str] | None = None) -> None:
        if source not in FALLBACK_ORDER:
            raise ValueError(f"a ground-truth reader reads cells: source one of {FALLBACK_ORDER}")
        if not 0.0 <= noise <= 1.0:
            raise ValueError("noise is a probability")
        self.name = source
        self.values = dict(values)
        self.confidence = confidence
        self.misread = dict(misread or {})
        self.second_pass = dict(second_pass or {})
        self.noise, self.seed, self.noise_penalty = noise, seed, noise_penalty
        self.colors = dict(colors or {})
        self.crop_refs = dict(crop_refs or {})

    def _confidence(self, field: str) -> float:
        c = self.confidence
        return float(c.get(field, 95.0) if isinstance(c, Mapping) else c)

    def _noisy(self, field: str, text: str) -> tuple[str, int]:
        if self.noise == 0.0:
            return text, 0
        state = int.from_bytes(hashlib.sha256(f"{self.seed}:{field}".encode()).digest()[:8], "big")
        out, hits = [], 0
        for ch in text:
            state = (state * 6364136223846793005 + 1442695040888963407) % 2**64
            if ch.isdigit() and (state >> 11) / 2**53 < self.noise:
                out.append(str((int(ch) + 1 + (state >> 3) % 9) % 10))
                hits += 1
            else:
                out.append(ch)
        return "".join(out), hits

    def read(self, field: str) -> Reading | None:
        if self.values.get(field) is None:
            return None
        text, hits = self._noisy(field, self.misread.get(field, self.values[field] or ""))
        confidence = max(0.0, self._confidence(field) - hits * self.noise_penalty)
        second = self.second_pass.get(field, text) if self.name == "ocr" else None
        return Reading(field, self.name, text, confidence, second, self.crop_refs.get(field), self.colors.get(field))


class GroundTruthApi:
    """An `ApiReader` over time series: `series[(symbol, field)]` is a list of (time, value text);
    a quote is the last value at or before `as_of`. `available=False` raises `ApiUnavailable`."""

    def __init__(self, series: Mapping[tuple[str, str], Sequence[tuple[datetime, str]]], *, available: bool = True):
        self.series = {k: sorted(v) for k, v in series.items()}
        self.available = available

    def quote(self, symbol: str, field: str, as_of: datetime) -> Reading | None:
        if not self.available:
            raise ApiUnavailable("market-data API unreachable")
        past = [v for t, v in self.series.get((symbol, field), ()) if t <= as_of]
        return Reading(f"{symbol}.{field}", "api", past[-1], EXACT_CONFIDENCE) if past else None


# --------------------------------------------------------------------------------------------
# Exports (2.8.1 CSV export, Grid copy)


@dataclass(frozen=True)
class ExportTable:
    """A parsed export: `kind` `file` (CSV) or `copy_all` (tab-separated clipboard text)."""

    kind: str
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if self.kind not in ("file", "copy_all"):
            raise ValueError(f"export kind {self.kind!r} (4.7 export step: file or copy_all)")

    @classmethod
    def parse(cls, text: str, kind: str) -> "ExportTable":
        """The first non-empty line is the header; CSV for a file, tab-separated for Copy All."""
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return cls(kind, (), ())
        if kind == "file":
            parsed = list(csv.reader(io.StringIO("\n".join(lines))))
        else:
            parsed = [ln.split("\t") for ln in lines]
        return cls(kind, tuple(h.strip() for h in parsed[0]), tuple(tuple(c.strip() for c in r) for r in parsed[1:]))

    def column(self, name: str) -> tuple[str, ...] | None:
        if name not in self.header:
            return None
        i = self.header.index(name)
        return tuple(r[i] if i < len(r) else "" for r in self.rows)


class ExportSource:
    """An `ExportReader` over an `ExportTable` (None: nothing arrived within the step timeout).
    `field_map` maps a region id to the (column, row) cell that holds the same value."""

    name = "export"

    def __init__(self, table: ExportTable | None, declared_header: Sequence[str], *,
                 kind: str | None = None, field_map: Mapping[str, tuple[str, int]] | None = None) -> None:
        if table is None and kind is None:
            raise ValueError("an export that produced nothing still names its kind")
        self.table = table
        self.kind = table.kind if table is not None else str(kind)
        self.declared_header = tuple(declared_header)
        self.field_map = dict(field_map or {})

    @property
    def produced(self) -> bool:
        return self.table is not None

    @property
    def header_matches(self) -> bool:
        """2.8.1: the header set equals the declared set (order is not compared)."""
        return self.table is not None and set(self.table.header) == set(self.declared_header)

    def column(self, name: str) -> tuple[str, ...] | None:
        return self.table.column(name) if self.table is not None and self.header_matches else None

    def read(self, field: str) -> Reading | None:
        if field not in self.field_map:
            return None
        col_name, row = self.field_map[field]
        col = self.column(col_name)
        if col is None or not 0 <= row < len(col):
            return None
        return Reading(field, "export", col[row], EXACT_CONFIDENCE)


# --------------------------------------------------------------------------------------------
# Acceptance and fallback (2.8.1 Extraction confidence)


def floor(fmt: str) -> float:
    """The confidence floor of a field type."""
    return FLOORS[fmt]


def problem(reading: Reading | None, fmt: str) -> str | None:
    """Why a read is unreadable (2.8.1), or None when it is accepted."""
    if reading is None or reading.text is None:
        return "nothing read"
    if reading.confidence < floor(fmt):
        return f"confidence {reading.confidence:.0f} below the {fmt} floor {floor(fmt):.0f}"
    if reading.second_pass is not None and digit_count(reading.text) != digit_count(reading.second_pass):
        return (f"3x and 2x passes disagree on digit count ({digit_count(reading.text)} in {reading.text!r}, "
                f"{digit_count(reading.second_pass)} in {reading.second_pass!r})")
    return None


@dataclass(frozen=True)
class Attempt:
    """One source tried for one field: what it read (None: nothing) and why it was rejected."""

    source: str
    reading: Reading | None
    rejected: str | None


@dataclass(frozen=True)
class SourceFallback:
    """Step event `oracle_source_fallback` (2.8.1): the declared source failed, another read the field."""

    field: str
    from_source: str
    to_source: str
    why: str

    def event(self) -> str:
        return f"oracle_source_fallback:{self.field}:{self.from_source}->{self.to_source}"


@dataclass(frozen=True)
class Extraction:
    """A typed extraction of one field: the accepted read (None when no source read it above its
    floor) and every attempt, declared source first. `to_record()` is the stored form (5.10:
    field, value, source, confidence)."""

    field: str
    fmt: str
    declared: str
    attempts: tuple[Attempt, ...]
    accepted: Reading | None = None

    @property
    def readable(self) -> bool:
        return self.accepted is not None

    @property
    def source(self) -> str | None:
        return self.accepted.source if self.accepted else None

    @property
    def text(self) -> str | None:
        return self.accepted.text if self.accepted else None

    @property
    def confidence(self) -> float | None:
        return self.accepted.confidence if self.accepted else None

    @property
    def crop_ref(self) -> str | None:
        for a in self.attempts:
            if a.reading is not None and a.reading.crop_ref:
                return a.reading.crop_ref
        return None

    @property
    def fallback(self) -> SourceFallback | None:
        if self.accepted is None or self.accepted.source == self.declared:
            return None
        why = next((a.rejected for a in self.attempts if a.source == self.declared and a.rejected), "unavailable")
        return SourceFallback(self.field, self.declared, self.accepted.source, why)

    def rows(self) -> list[str]:
        return rows(self.text)

    def value(self) -> Value:
        """The accepted text parsed as `fmt` (raises `FormatError`; call only when readable)."""
        return parse(self.fmt, self.text)

    def reads(self) -> str:
        """Every read tried, for `detail` and the reporter: 'ocr '12.5' (confidence 60): ...; jab: nothing read'."""
        return "; ".join(f"{a.source} {a.reading.describe() if a.reading else 'nothing'}"
                         + (f": {a.rejected}" if a.rejected else "") for a in self.attempts)

    def to_record(self) -> dict[str, object]:
        return {"field": self.field, "format": self.fmt, "value": self.text, "source": self.source,
                "confidence": self.confidence, "crop_ref": self.crop_ref, "readable": self.readable}


def literal(field: str, fmt: str, text: str, source: str = "ocr") -> Extraction:
    """A stored or known text as a readable extraction (offline rule re-runs, 2.8.1)."""
    reading = Reading(field, source, text, EXACT_CONFIDENCE)
    return Extraction(field, fmt, source, (Attempt(source, reading, None),), reading)


def extract(field: str, fmt: str, sources: Mapping[str, ValueSource], declared: str = "ocr", *,
            exclude: frozenset[str] = frozenset()) -> Extraction:
    """Read `field` from the declared source, then the alternates (`FALLBACK_ORDER`), accepting the
    first read above its floor whose OCR passes agree. `exclude` names sources that may not be used
    (OCR after a calibration miss); a `copy_all` export whose header does not match is skipped."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown field format {fmt!r}")
    order = [declared] + [s for s in FALLBACK_ORDER if s != declared]
    attempts: list[Attempt] = []
    why: str | None
    for name in order:
        src = sources.get(name)
        if src is None or name in exclude:
            continue
        if isinstance(src, ExportReader) and not src.header_matches:
            why = ("nothing exported within the step timeout" if not src.produced
                   else "header differs from the declared set")
            attempts.append(Attempt(name, None, why))
            continue
        reading = src.read(field)
        why = problem(reading, fmt)
        attempts.append(Attempt(name, reading, why))
        if why is None:
            return Extraction(field, fmt, declared, tuple(attempts), reading)
    return Extraction(field, fmt, declared, tuple(attempts))


# --------------------------------------------------------------------------------------------
# Calibration panel (2.8.1, preflight H9, gate canary_flow_passed)


@dataclass(frozen=True)
class CalibrationSample:
    """A checked-in crop with a known value."""

    field: str
    fmt: str
    expected: str
    crop_ref: str | None = None

    def __post_init__(self) -> None:
        if self.fmt not in FORMATS:
            raise ValueError(f"calibration sample {self.field}: unknown format {self.fmt!r}")


@dataclass(frozen=True)
class CalibrationPanel:
    """The OCR calibration panel: at least one sample of every field type (2.8.1)."""

    samples: tuple[CalibrationSample, ...]

    def __post_init__(self) -> None:
        missing = [f for f in FORMATS if f not in {s.fmt for s in self.samples}]
        if missing:
            raise ValueError(f"the calibration panel lacks a sample of {', '.join(missing)} (2.8.1)")


@dataclass(frozen=True)
class CalibrationMiss:
    field: str
    expected: str
    read: str | None
    why: str


@dataclass(frozen=True)
class CalibrationResult:
    """`passed` is False on any miss: at preflight the run is `aborted_harness (reason:
    preflight.H9)`; at `canary_flow_passed` every OCR assertion of the run is `harness_error
    (reason: ocr_calibration)` (pass `ocr_calibrated=False` to `evaluate_assertions`)."""

    misses: tuple[CalibrationMiss, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return not self.misses


def check_calibration(panel: CalibrationPanel, ocr: ValueSource) -> CalibrationResult:
    """OCR every sample of the panel; a read is a hit only when it is exact (H9)."""
    misses = []
    for s in panel.samples:
        reading = ocr.read(s.field)
        why = problem(reading, s.fmt)
        if why is None and (reading.text or "").strip() != s.expected.strip():   # type: ignore[union-attr]
            why = "read differs from the known value"
        if why is not None:
            misses.append(CalibrationMiss(s.field, s.expected, reading.text if reading else None, why))
    return CalibrationResult(tuple(misses))
