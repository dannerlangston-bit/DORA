"""The Tesseract OCR engine adapter (5.18; 2.8.1, 5.5 rung 3, 5.10, 6.10 stage 2, 12.7 H9).

`TesseractOcr` is the Oracle's `sources.OcrReader` and the locator's and redaction sweep's `WordOcr`, over the
`tesseract` command-line program run in a subprocess (no Python dependency; 5.18 names pytesseract, which is the same
program behind a wrapper):

- `read(field)`: the field's crop (`OcrCrop`, from the caller's `crops`) prepared at 3x (`prepare`: grayscale, inverted
  when the theme is dark, upscaled) and read with the format's character whitelist (`WHITELISTS`), then read again at
  2x: the `Reading` carries the 3x text and its confidence, the 2x text as `second_pass` (the digit-count check of
  2.8.1 is `sources.problem`'s), the crop reference and the dominant text color (`text_color`, RGB, for INV-15).
- `words(image)`: every word of an image the caller already prepared (`locator.prepare_ocr`, the sweep's 3x upscale)
  with its box in that image's pixels and its confidence.
- `parse_tsv` and `text_and_confidence` turn Tesseract's word-level TSV into words, lines and one confidence; both are
  pure and tested on recorded output.

Choices (reported):

1. A read's confidence is the lowest word confidence of the 3x pass (a value is as sure as its least sure word),
   clamped to 0..100. A crop where Tesseract finds no word reads as `""` with confidence 0: Tesseract gives no
   confidence for "nothing there", and an empty money cell read silently would be the quiet pass 2.8.1 forbids, so
   the floor makes it unreadable and the Oracle tries the alternates.
2. Inversion: a crop whose median gray is below 128 is dark-themed (light text on a dark ground) and is inverted, so
   Tesseract sees dark text on light; `OcrCrop.dark` overrides the guess.
3. Upscaling is bicubic, as `locator.prepare_ocr`; the `--dpi` passed is 96 times the scale, the rig's 96 dpi (7.1).
4. Whitelists follow `oracle.values`' display conventions: money `0-9 $ , . - + ( )`, int without `.` and `$`, percent
   with `%`, price with `@` and the letters of the order-type suffixes, date with `/ -` and the month letters, time
   with `:` and the letters of AM, PM, ET, EST and EDT; text has none. Typed fields also switch the dictionaries off
   (`load_system_dawg`, `load_freq_dawg`), which bias digits toward words.
5. Page segmentation: `--psm 7` (one line) for a single-line crop, `--psm 6` (a block) for a multi-row region, and
   `--psm 11` (sparse text) for `words`; the LSTM engine (`--oem 1`).
6. An engine failure (non-zero exit, timeout, the program vanished) reads nothing for that field (None), which
   `sources.extract` treats as an unreadable source; a failed 2x pass leaves `second_pass` None, which
   `sources.problem` rejects for a typed field. `words` raises `OcrEngineError` instead: an empty word list would
   tell the redaction sweep (6.10 stage 2) that a frame holds no identifier. A missing program raises
   `TesseractUnavailable` when the adapter is built, so preflight H9 fails loudly instead of every field quietly
   falling back.

Verified here: the preprocessing, the command lines, the TSV parsing and the confidence mapping, on synthetic crops
and recorded TSV. A test reads synth-rendered digits through the real program when `tesseract` is installed and is
skipped otherwise. Only Phase 0 on a rig verifies the engine's accuracy on the pinned font at the pinned scale (the
calibration panel, H9) and chooses between Tesseract and PaddleOCR (5.18).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from ..capture.redaction import OcrWord
from ..diff.regions import Rect
from .sources import Reading
from .values import FORMATS, ORDER_TYPE_SUFFIXES

UPSCALE, SECOND_PASS = 3, 2                                    # 2.8.1
RIG_DPI = 96                                                   # 7.1
DARK_MEDIAN = 128                                              # choice 2
TIMEOUT_S = 10.0
_MONTHS = "JANFEBMARAPRMAYJUNJULAUGSEPOCTNOVDEC"


def _letters(*words: str) -> str:
    return "".join(sorted({c for w in words for c in w if c.isalpha()}))


#: Character whitelists per field format (choice 4); None: no whitelist.
WHITELISTS: dict[str, str | None] = {
    "money": "0123456789$,.-+()",
    "price": "0123456789$,.-+()@" + _letters(*ORDER_TYPE_SUFFIXES),
    "int": "0123456789,-+()",
    "percent": "0123456789,.-+()%",
    "date": "0123456789/-" + _letters(_MONTHS),
    "time": "0123456789:" + _letters("AM", "PM", "ET", "EST", "EDT"),
    "text": None,
}


class TesseractUnavailable(RuntimeError):
    """The `tesseract` program is not installed where the adapter looks (choice 6)."""


class OcrEngineError(RuntimeError):
    """One run of the program failed (non-zero exit, timeout, the program vanished)."""


@dataclass(frozen=True)
class OcrCrop:
    """A field's crop at screen scale (BGR or gray) with its format; `dark` None guesses the theme (choice 2)."""

    image: Any
    fmt: str
    ref: str | None = None
    single_line: bool = True
    dark: bool | None = None


@dataclass(frozen=True)
class TsvWord:
    """One word row (level 5) of Tesseract's TSV."""

    text: str
    conf: float
    left: int
    top: int
    width: int
    height: int
    line: tuple[int, int, int]                                 # block, paragraph, line


def parse_tsv(tsv: str) -> list[TsvWord]:
    """The words of a TSV output, in order: level-5 rows with a confidence and non-blank text."""
    out = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 11 or cols[0] != "5":
            continue
        text = cols[11].strip() if len(cols) > 11 else ""
        try:
            conf = float(cols[10])
            block, par, line, left, top, width, height = (int(cols[i]) for i in (2, 3, 4, 6, 7, 8, 9))
        except ValueError:
            continue
        if conf < 0 or not text:
            continue
        out.append(TsvWord(text, conf, left, top, width, height, (block, par, line)))
    return out


def text_and_confidence(words: Sequence[TsvWord]) -> tuple[str, float]:
    """Words joined by a space within a line and lines by a newline, with the lowest word confidence (choice 1)."""
    if not words:
        return "", 0.0
    lines: dict[tuple[int, int, int], list[str]] = {}
    for w in words:
        lines.setdefault(w.line, []).append(w.text)
    text = "\n".join(" ".join(ws) for ws in lines.values())
    return text, max(0.0, min(100.0, min(w.conf for w in words)))


def is_dark(gray: np.ndarray) -> bool:
    """A dark-themed crop: median gray below 128 (choice 2)."""
    return float(np.median(gray)) < DARK_MEDIAN


def _gray(image: np.ndarray) -> np.ndarray:
    return image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def prepare(image: np.ndarray, scale: int, *, dark: bool | None = None) -> np.ndarray:
    """The OCR input of one pass: grayscale, inverted when dark (choice 2), upscaled by `scale` (choice 3)."""
    gray = _gray(image)
    if dark if dark is not None else is_dark(gray):
        gray = 255 - gray
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def text_color(image: np.ndarray, *, dark: bool | None = None) -> tuple[int, int, int] | None:
    """The dominant text color as RGB: the median of the pixels on the text side of an Otsu threshold (light pixels
    on a dark crop, dark ones on a light crop); None for a gray crop or one with no text pixels."""
    if image.ndim != 3:
        return None
    gray = _gray(image)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = mask > 0 if (dark if dark is not None else is_dark(gray)) else mask == 0
    if not text.any() or text.all():
        return None
    b, g, r = (int(np.median(image[:, :, i][text])) for i in range(3))
    return r, g, b


def parse_version(output: str) -> str | None:
    """The version of `tesseract --version` output ('tesseract 5.3.0' or 'tesseract v5.0.0-alpha')."""
    m = re.search(r"tesseract\s+v?(\d+(?:\.\d+)*\S*)", output)
    return m.group(1) if m else None


def _subprocess(args: list[str], stdin: bytes, timeout: float) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OcrEngineError(f"{args[0]}: {e}") from e
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


Runner = Callable[[list[str], bytes, float], tuple[int, str, str]]


class TesseractOcr:
    """`sources.OcrReader` and `WordOcr` over the `tesseract` program (module docstring). `crops` gives a field's
    crop for `read`; `run` runs the program (a subprocess by default; tests pass recorded output)."""

    name = "ocr"

    def __init__(self, crops: Callable[[str], OcrCrop | None] | None = None, *, binary: str = "tesseract",
                 lang: str = "eng", run: Runner | None = None, timeout_s: float = TIMEOUT_S):
        found = binary if run is not None else shutil.which(binary)
        if found is None:
            raise TesseractUnavailable(f"{binary!r} not found on PATH; install Tesseract or pass its full path")
        self.binary, self.lang, self.timeout_s = found, lang, timeout_s
        self.crops = crops
        self.run = run or _subprocess

    def version(self) -> str | None:
        """The engine version, for the fingerprint's `harness.ocr_engine` (4.12)."""
        code, out, err = self.run([self.binary, "--version"], b"", self.timeout_s)
        return parse_version(out + "\n" + err) if code == 0 else None

    def command(self, *, psm: int, scale: int, fmt: str | None) -> list[str]:
        """The command line of one pass (choices 3 to 5)."""
        args = [self.binary, "stdin", "stdout", "--oem", "1", "--psm", str(psm), "--dpi", str(RIG_DPI * scale),
                "-l", self.lang]
        whitelist = WHITELISTS[fmt] if fmt is not None else None
        if whitelist is not None:
            args += ["-c", f"tessedit_char_whitelist={whitelist}", "-c", "load_system_dawg=0",
                     "-c", "load_freq_dawg=0"]
        return args + ["tsv"]

    def _tsv(self, image: np.ndarray, args: list[str]) -> list[TsvWord]:
        ok, png = cv2.imencode(".png", image)
        if not ok:
            raise OcrEngineError("the crop could not be encoded")
        code, out, err = self.run(args, png.tobytes(), self.timeout_s)
        if code != 0:
            raise OcrEngineError(f"tesseract exited {code}: {err.strip()[:200]}")
        return parse_tsv(out)

    def _pass(self, crop: OcrCrop, scale: int) -> tuple[str, float] | None:
        try:
            words = self._tsv(prepare(crop.image, scale, dark=crop.dark),
                              self.command(psm=7 if crop.single_line else 6, scale=scale, fmt=crop.fmt))
        except OcrEngineError:
            return None
        return text_and_confidence(words)

    def read(self, field: str) -> Reading | None:
        """The field's 3x read with its 2x second pass (module docstring); None when there is no crop or the 3x pass
        failed (choice 6)."""
        crop = self.crops(field) if self.crops is not None else None
        if crop is None:
            return None
        if crop.fmt not in FORMATS:
            raise ValueError(f"field {field}: unknown format {crop.fmt!r}")
        first = self._pass(crop, UPSCALE)
        if first is None:
            return None
        second = self._pass(crop, SECOND_PASS)
        return Reading(field, "ocr", first[0], first[1], second[0] if second else None, crop.ref,
                       text_color(crop.image, dark=crop.dark))

    def words(self, image: np.ndarray) -> list[OcrWord]:
        """Every word of an image the caller already prepared at 3x, boxes in its pixels (`WordOcr`); raises
        `OcrEngineError` when the engine failed (choice 6)."""
        rows = self._tsv(image, self.command(psm=11, scale=UPSCALE, fmt=None))
        return [OcrWord(w.text, Rect(w.left, w.top, w.width, w.height), w.conf) for w in rows]
