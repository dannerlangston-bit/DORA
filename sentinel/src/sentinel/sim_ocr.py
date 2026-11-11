"""Pixel-true OCR for the synthetic screens (2.7 Bounds, Branch B; 2.8.1 OCR reader). Simulation and tests only.

The simulated bounds source and the simulated Oracle read what `sentinel.synth` drew. A canary mutant (10.3)
changes pixels, not that ground truth, so the parts of a frame a mutant touched are read again from the pixels with
`read_words`: a clipped label reads as its visible prefix, a blanked value reads as nothing, a transplanted digit reads
as the glyph that is there now, and pasted text ("Loading...", a planted identifier, an exception dialog title) is read
like any other text.

`read_words(image, font=SYNTH_FONT, rect=None)` returns `OcrWord`s (text, box, confidence 0 to 100, cap height,
baseline, ink color). It knows the face synth draws with (an OCR engine trained on the app's font): the glyph bank of a
`Font` is the face rendered by OpenCV, one alpha stamp per printable ASCII character, at the integer advances the
renderer uses. Reading a line is analysis by synthesis: a dynamic program over pen positions explains the line's alpha
(the pixels' blend between the line's background and ink colors) as a sequence of stamps and unexplained columns, at
least squared error; unexplained ink costs what it weighs, so a partial glyph is left unread rather than guessed.

Choices (simulation fidelity, reported where they shape a result):

1. Segmentation. Ink is a pixel more than `INK_DELTA` levels from the median of its 7x7 neighbourhood (thin strokes
   leave the median, flat fills and straight fill edges do not), less straight runs of `LINE_PX` or more (frame,
   border and grid lines); ink closer than `JOIN_PX` columns or rows joins one blob, and a blob is one line of text,
   words split later by the columns no glyph explains. A blob whose box is mostly ink (`SOLID`) or holds a solid
   patch (`STROKE_PX` from its middle to its edge) is a shape, never text. `read_words(rects=)` reads every line that
   touches one of the rects, followed `REACH` past their edges.
2. Background and ink color of a blob: the most frequent color of its box, and the median of the pixels at least
   three quarters as far from it as the farthest. A pixel's alpha is its projection on the background-to-ink segment.
3. Size. Text drawn by synth uses `SYNTH_FONT`; text a mutant pastes or shrinks has another size. A blob whose ink
   height does not fit the synth font (or that the synth font cannot read) is read with the same face at every scale
   whose cap height fits, descenders up to 40 percent of the height allowed, 8-connected then anti-aliased strokes;
   the reading that leaves the least ink unexplained wins, and an `EXACT` reading ends the search.
4. Acceptance. A glyph is kept when its stamp explains at least `GLYPH_QUALITY` of the ink in its own columns, judged
   against the whole line rebuilt from the chosen stamps (1 - squared error / (observed + rebuilt energy)); a word
   needs one letter or numeral and a confidence of `WORD_CONFIDENCE`, so graphics (candles, study lines, focus
   rings, a mirrored button) never read as words. Word confidence is 100 x the same ratio over the word's glyphs.
5. Boxes. A word's box is the text box synth records for the same text at the same pen and baseline
   (`cv2.getTextSize`: the advance sum plus one, from the cap line minus one to the baseline plus the descent), so a
   re-read word and a drawn word share one box convention; the cap height is that text height.
6. Three dots (canary 10.3 `clipped_label` ellipsis) of one or two pixels on or just above the baseline, two columns
   apart and within `DOTS_REACH` columns of the word's last glyph (past a clipped glyph's remains), read as "...".
7. Pens. Glyphs drawn as one string sit at the renderer's integer advances; a glyph pasted into a word (a
   transplanted digit) may sit a column off. The program lets a glyph end one column early at `JITTER`, and a word
   goes on across a gap narrower than the face's space advance.
8. Small text. A line shorter than the synth font's cap height (a row a mutant shrank) is scaled up to that
   height and read with the synth font; such a read is kept from `SMALL_QUALITY`, as an engine that upsamples small
   text 3x (2.7 Bounds) still reads it at lower confidence. Its box and cap height are the read's, scaled back. Only
   a blob at least `SMALL_MIN_H` tall and twice as wide is such a line, and only words of two or more letters or
   numerals are kept, so specks and corners never read as text.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Sequence

import cv2
import numpy as np

INK_DELTA = 40          # choice 1: levels from the local median that make a pixel ink
JOIN_PX = 3             # choice 1: ink this close (columns or rows) is one blob
LINE_PX = 21            # choice 1: a straight run of ink this long is a frame or grid line, not text
REACH = (96, 12)        # choice 1: how far past a rect's edge the lines that touch it are followed
GLYPH_QUALITY = 0.85    # choice 4
WORD_CONFIDENCE = 96.0  # choice 4: a word reads at this confidence or not at all
EXACT = 99.0            # choice 3: a reading this confident that explains the line ends the size search
DOTS_REACH = 8          # choice 6
JITTER = 0.5            # choice 7: cost of a glyph that ends one column early
TWIN = 0.05             # choice 4: glyphs whose errors differ by less than this share of a stamp are near twins
TWIN_STAMPS = 0.12      # choice 4: stamps closer than this share of their energy are twins worth a second look
SMALL_QUALITY = 0.80    # choice 8: glyph quality (and word confidence / 100) of a line read scaled up
SMALL_MIN_H = 4         # choice 8: a squeezed line is at least this tall and twice as wide
SKIP_EPS = 0.02         # cost of an empty column the program leaves unexplained
MIN_BLOB_H, MAX_BLOB_H = 3, 30
STROKE_PX = 2.5         # choice 1: text strokes are thinner than this from their middle to their edge
SOLID = 0.6             # choice 1: a blob whose box is more than this share ink is a shape, not text
CHARSET = tuple(chr(c) for c in range(33, 127))


@dataclass(frozen=True)
class Font:
    """An OpenCV face at one scale and stroke type: what synth draws text with."""

    face: int = cv2.FONT_HERSHEY_SIMPLEX
    scale: float = 0.36
    line_type: int = cv2.LINE_AA


SYNTH_FONT = Font()


@dataclass(frozen=True)
class OcrWord:
    """One word read from pixels: `bbox` is (x, y, w, h) in frame pixels (choice 5)."""

    text: str
    bbox: tuple[int, int, int, int]
    confidence: float
    cap_height: float
    baseline: int
    color: tuple[int, int, int]


@dataclass(frozen=True)
class _Bank:
    """The glyph stamps of one font: rows `ascent` above to `descent` below the baseline."""

    font: Font
    ascent: int
    descent: int
    chars: tuple[str, ...]
    advances: tuple[int, ...]
    stamps: tuple[np.ndarray, ...]       # float32 alpha, shape (ascent + descent + 1, width), column 0 at the pen
    space: int
    cap: int                             # ink rows of a capital letter
    widths: np.ndarray                   # stamp widths
    energy: np.ndarray                   # sum of squared alpha per stamp; 0 marks an empty stamp
    twins: tuple[tuple[int, ...], ...]   # per character: the characters of its advance whose stamps are near it


def _render(text: str, font: Font, pen: int, baseline: int, size: tuple[int, int]) -> np.ndarray:
    canvas = np.zeros((size[1], size[0]), np.uint8)
    cv2.putText(canvas, text, (pen, baseline), font.face, font.scale, 255, 1, font.line_type)
    return canvas


def _advance(c: str, font: Font) -> int:
    return int(round(cv2.getTextSize(c * 100, font.face, font.scale, 1)[0][0] / 100))


@functools.lru_cache(maxsize=128)
def _bank(font: Font) -> _Bank:
    """Render every printable character once (choice 3: the face at this scale and stroke type)."""
    th = cv2.getTextSize("Hg|", font.face, font.scale, 1)[0][1]
    pad = th + 4
    baseline = 2 * pad
    raw = []
    top, bottom = baseline, baseline
    for c in CHARSET:
        adv = _advance(c, font)
        img = _render(c, font, pad, baseline, (adv + 3 * pad, 3 * pad))
        rows, cols = np.flatnonzero(img.any(axis=1)), np.flatnonzero(img.any(axis=0))
        if rows.size:
            top, bottom = min(top, int(rows[0])), max(bottom, int(rows[-1]))
            width = max(adv + 1, int(cols[-1]) - pad + 1)
        else:
            width = adv + 1
        raw.append((c, adv, img, width))
    ascent, descent = baseline - top, bottom - baseline
    stamps, advs = [], []
    for c, adv, img, width in raw:
        stamps.append(img[baseline - ascent:baseline + descent + 1, pad:pad + width].astype(np.float32) / 255.0)
        advs.append(adv)
    h = _render("H", font, pad, baseline, (3 * pad, 3 * pad))
    hr = np.flatnonzero(h.any(axis=1))
    energy = [float(np.square(st).sum()) for st in stamps]

    def near(i: int, j: int) -> bool:
        a, b = stamps[i], stamps[j]
        return advs[i] == advs[j] and a.shape == b.shape and energy[i] + energy[j] > 0 and \
            float(np.square(a - b).sum()) <= TWIN_STAMPS * (energy[i] + energy[j])
    twins = tuple(tuple(j for j in range(len(stamps)) if j == i or near(i, j)) for i in range(len(stamps)))
    return _Bank(font, ascent, descent, CHARSET, tuple(advs), tuple(stamps), _advance(" ", font),
                 int(hr[-1] - hr[0] + 1) if hr.size else th, np.array([st.shape[1] for st in stamps]),
                 np.array(energy), twins)


@functools.lru_cache(maxsize=4)
def _cap_table(face: int, line_type: int) -> tuple[tuple[float, int], ...]:
    """(scale, cap height in px) for the scales the reader tries on text of another size (choice 3)."""
    out = []
    for k in range(15, 161):
        s = k / 100
        img = _render("H", Font(face, s, line_type), 4, 4 * 40, (200, 200))
        rows = np.flatnonzero(img.any(axis=1))
        if rows.size:
            out.append((s, int(rows[-1] - rows[0] + 1)))
    return tuple(out)


# ------------------------------------------------------------------------------------------------
# Segmentation and color


def _ink_mask(image: np.ndarray, rect: tuple[int, int, int, int]) -> tuple[np.ndarray, int, int]:
    """Ink of `rect` grown by `REACH` columns and rows (choice 1), so a line that crosses the rect's edge is read
    whole, with the offset of the returned mask in frame pixels."""
    H, W = image.shape[:2]
    x, y, w, h = rect
    x0, y0 = max(0, x - REACH[0]), max(0, y - REACH[1])
    x1, y1 = min(W, x + w + REACH[0]), min(H, y + h + REACH[1])
    sub = np.ascontiguousarray(image[y0:y1, x0:x1])
    if sub.shape[0] < 3 or sub.shape[1] < 3:
        return np.zeros(sub.shape[:2], bool), x0, y0
    med = cv2.medianBlur(sub, 7)
    ink = (np.abs(sub.astype(np.int16) - med.astype(np.int16)).max(axis=2) > INK_DELTA).astype(np.uint8)
    lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, LINE_PX), np.uint8)) | \
        cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((LINE_PX, 1), np.uint8))
    return (ink & ~lines).astype(bool), x0, y0     # frame, border and grid lines are not text


def _blobs(ink: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Ink boxes (x, y, w, h) of one line each (choice 1), in mask coordinates."""
    if not ink.any():
        return []
    joined = cv2.dilate(ink.astype(np.uint8), np.ones((JOIN_PX, JOIN_PX + 1), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    out = []
    for k in range(1, n):
        m = (labels == k) & ink
        ys, xs = np.nonzero(m)
        if ys.size == 0:
            continue
        x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        if not MIN_BLOB_H <= y1 - y0 <= MAX_BLOB_H:
            continue
        out.append((x0, y0, x1 - x0, y1 - y0))
    return sorted(out, key=lambda b: (b[1], b[0]))


def _thick(pixels: np.ndarray) -> bool:
    """True when a blob's box holds a solid patch: a pixel of the non-background mask farther than `STROKE_PX` from
    the background (a candle, an icon or a bar, never a stroke of text; choice 1). The background is the box's most
    frequent color."""
    bg, _ = _colors(pixels)
    mask = (np.abs(pixels.astype(np.float32) - bg).max(axis=2) > INK_DELTA).astype(np.uint8)
    return float(cv2.distanceTransform(np.pad(mask, 1), cv2.DIST_L2, 3).max()) > STROKE_PX


def _colors(pixels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Background (most frequent color) and ink color of a blob's pixels (choice 2)."""
    flat = pixels.reshape(-1, 3).astype(np.int32)
    packed = (flat[:, 0] << 16) | (flat[:, 1] << 8) | flat[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    v = int(values[int(np.argmax(counts))])
    bg = np.array([(v >> 16) & 255, (v >> 8) & 255, v & 255], np.float32)
    dist = np.abs(flat - bg).max(axis=1)
    far = flat[dist >= 0.75 * dist.max()]
    return bg, np.median(far, axis=0).astype(np.float32)


def _alpha(pixels: np.ndarray, bg: np.ndarray, ink: np.ndarray) -> np.ndarray:
    d = ink - bg
    n = float(d @ d)
    if n < 1.0:
        return np.zeros(pixels.shape[:2], np.float32)
    a = ((pixels.astype(np.float32) - bg) @ d) / n
    a = np.clip(a, 0.0, 1.0)
    a[a < 0.06] = 0.0
    return a


# ------------------------------------------------------------------------------------------------
# Decoding one line


@dataclass(frozen=True)
class _Glyph:
    pen: int            # in band columns
    char: str
    quality: float
    energy: float       # observed plus stamp energy over the glyph's window
    error: float


def _decode(alpha: np.ndarray, bank: _Bank) -> list[_Glyph]:
    """The least-error explanation of a band (rows: the bank's, columns: pen positions) as stamps and skips."""
    H, W = alpha.shape
    maxw = int(bank.widths.max())
    padded = np.zeros((H, W + maxw + 1), np.float32)
    padded[:, :W] = alpha
    sq = padded * padded
    colsq = np.concatenate(([0.0], np.cumsum(sq.sum(axis=0)))).astype(np.float32)
    pens = np.arange(W)
    obs = colsq[pens[None, :] + bank.widths[:, None]] - colsq[pens][None, :]
    corr = np.zeros_like(obs)
    for k, stamp in enumerate(bank.stamps):
        if bank.energy[k] > 0:
            corr[k] = cv2.matchTemplate(padded, stamp, cv2.TM_CCORR)[0, :W]
    cost = np.maximum(obs - 2.0 * corr + bank.energy[:, None], 0.0)
    cost[bank.energy == 0] = np.inf
    skip = sq[:, :W].sum(axis=0) + SKIP_EPS
    adv = np.array(bank.advances)
    groups = []                                   # per advance: the cheapest character at every pen
    for a in sorted(set(bank.advances)):
        ks = np.flatnonzero(adv == a)
        groups.append((a, cost[ks].min(axis=0).tolist(), ks[cost[ks].argmin(axis=0)].tolist()))
    skips = skip.tolist()
    best = [float("inf")] * (W + 1)
    best[0] = 0.0
    back: list[tuple[int, int]] = [(-1, -1)] * (W + 1)    # (previous pen, char index or -1 for a skip)
    for p in range(W):
        b = best[p]
        if b == float("inf"):
            continue
        if b + skips[p] < best[p + 1]:
            best[p + 1], back[p + 1] = b + skips[p], (p, -1)
        for a, gmin, garg in groups:
            q = p + a
            if q <= W and b + gmin[p] < best[q]:
                best[q], back[q] = b + gmin[p], (p, garg[p])
            if a > 1 and q - 1 <= W and b + gmin[p] + JITTER < best[q - 1]:     # the next glyph overlaps by a column
                best[q - 1], back[q - 1] = b + gmin[p] + JITTER, (p, garg[p])
    chosen: list[tuple[int, int]] = []
    q = W
    while q > 0:
        p, k = back[q]
        if k >= 0:
            chosen.append((p, k))
        q = p
    chosen.reverse()
    # each glyph is judged on its own columns against the line rebuilt from every chosen stamp, so the ink a
    # neighbour spills into those columns is explained by that neighbour, not charged to the glyph
    def rebuilt(skip: int) -> np.ndarray:
        out = np.zeros_like(padded)
        for i, (p, k) in enumerate(chosen):
            if i != skip:
                st = bank.stamps[k]
                np.maximum(out[:, p:p + st.shape[1]], st, out=out[:, p:p + st.shape[1]])
        return out

    # near twins ('l' and 'I', 'O' and 'Q') differ by less than a neighbour's spill: a glyph with twins is chosen
    # again among them against the line rebuilt from the others
    for i, (p, k) in enumerate(chosen):
        same = bank.twins[k]
        if len(same) == 1:
            continue
        others = rebuilt(i)

        def err(j: int) -> float:
            st = bank.stamps[j]
            win = slice(p, p + st.shape[1])
            return float(np.square(padded[:, win] - np.maximum(others[:, win], st)).sum())
        errs = {j: err(j) for j in same}
        top = min(errs.values())
        near = [j for j in same if errs[j] <= top + TWIN * float(bank.energy[j])]
        # among near twins the letter case of the word's other letters decides ('Delete', not 'DeIete')
        ctx = [bank.chars[c] for _, c in chosen[max(0, i - 2):i] + chosen[i + 1:i + 3] if bank.chars[c].isalpha()]
        lower = sum(c.islower() for c in ctx) > sum(c.isupper() for c in ctx)
        best = min(near, key=lambda j: (errs[j], j != k))
        cased = [j for j in near if bank.chars[j].isalpha() and bank.chars[j].islower() == lower] \
            if bank.chars[best].isalpha() else []
        chosen[i] = (p, min(cased, key=lambda j: errs[j]) if cased else best)
    recon = rebuilt(-1)
    path = []
    for p, k in chosen:
        cols = slice(p, p + max(1, bank.advances[k]))
        obs, rec = padded[:, cols], recon[:, cols]
        e = float((obs * obs).sum() + (rec * rec).sum())
        err = float(np.square(obs - rec).sum())
        path.append(_Glyph(p, bank.chars[k], 1.0 - err / e if e > 0 else 0.0, e, err))
    return path


def _dots(alpha: np.ndarray, bank: _Bank, after: int) -> int:
    """Choice 6: the column after three 1 or 2 px dots starting within `DOTS_REACH` columns of `after` (past the
    remains of a clipped glyph), two columns apart; -1 when none."""
    H, W = alpha.shape
    base = bank.ascent
    rows = alpha[max(0, base - 3):min(H, base + 2)]
    for start in range(after, min(W, after + DOTS_REACH)):
        hits = [start + 2 * k for k in range(3)]
        if hits[-1] + 1 >= W:
            break
        if all(rows[:, h].max() >= 0.5 for h in hits) and all(rows[:, h + 1].max() < 0.5 for h in hits) and \
                alpha[:max(0, base - 3), start:hits[-1] + 2].max(initial=0.0) < 0.3:
            return hits[-1] + 2
    return -1


def _words_of(glyphs: Sequence[_Glyph], alpha: np.ndarray, bank: _Bank, quality: float = GLYPH_QUALITY,
              confidence: float = WORD_CONFIDENCE) -> list[tuple[str, int, float, float]]:
    """Group kept glyphs into words (text, first pen, error, energy): contiguous pens make a word (choice 4)."""
    out: list[tuple[str, int, float, float]] = []
    cur: list[_Glyph] = []

    def flush() -> None:
        energy = sum(g.energy for g in cur)
        if cur and any(g.char.isalnum() for g in cur) and \
                100.0 * (1.0 - sum(g.error for g in cur) / energy) >= confidence:
            text = "".join(g.char for g in cur)
            end = cur[-1].pen + bank.advances[bank.chars.index(cur[-1].char)]
            stop = _dots(alpha, bank, end)
            if stop > 0 and not text.endswith("..."):
                text += "..."
            out.append((text, cur[0].pen, sum(g.error for g in cur), sum(g.energy for g in cur)))
        cur.clear()

    for g in glyphs:
        if g.quality < quality:
            flush()
            continue
        if cur and not -1 <= g.pen - (cur[-1].pen + bank.advances[bank.chars.index(cur[-1].char)]) < bank.space:
            flush()
        cur.append(g)
    flush()
    return out


def _band(image: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> tuple[np.ndarray, np.ndarray]:
    """The alpha of a frame window (zero outside the frame) and the window's ink color (choice 2)."""
    H, W = image.shape[:2]
    band = np.zeros((y1 - y0, x1 - x0, 3), np.uint8)
    sy0, sy1, sx0, sx1 = max(0, y0), min(H, y1), max(0, x0), min(W, x1)
    inside = np.zeros(band.shape[:2], bool)
    if sy1 > sy0 and sx1 > sx0:
        band[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = image[sy0:sy1, sx0:sx1]
        inside[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = True
    if not inside.any():
        return np.zeros(band.shape[:2], np.float32), np.zeros(3, np.float32)
    bg, ink = _colors(band[inside])
    band[~inside] = bg.astype(np.uint8)
    return _alpha(band, bg, ink), ink


def _read_blob(image: np.ndarray, box: tuple[int, int, int, int], fonts: Sequence[Font],
               native: bool) -> list[OcrWord]:
    """The words of one blob (frame coordinates), read with the font and baseline of least error; the synth font
    (`native`) tries every baseline a descender allows, another size the cap line and two bottoms."""
    bx, by, bw, bh = box
    best: tuple[float, list[OcrWord]] | None = None
    for font in fonts:
        bank = _bank(font)
        for baseline in sorted({by + bh - d for d in range(0, bank.descent + 2)} | {by + bank.cap}
                               if native else {by + bank.cap, by + bh, by + bh - bank.descent}):
            x0 = bx - 2
            alpha, ink = _band(image, x0, baseline - bank.ascent, bx + bw + 3, baseline + bank.descent + 1)
            if not alpha.any():
                continue
            glyphs = _decode(alpha, bank)
            words = _words_of(glyphs, alpha, bank)
            total = float(np.square(alpha).sum())
            explained = sum(e - err for _, _, err, e in words) / 2
            score = total - explained          # ink left unexplained, the least wins
            found = []
            for text, pen, err, energy in words:
                (tw, th), base = cv2.getTextSize(text, font.face, font.scale, 1)
                x = x0 + pen
                found.append(OcrWord(text, (x, baseline - th - 1, tw, th + base + 1),
                                     round(100.0 * max(0.0, 1.0 - err / energy), 1) if energy > 0 else 0.0,
                                     float(th), baseline, tuple(int(v) for v in ink.round())))
            if found and (best is None or score < best[0] - 1e-6):
                best = (score, found)
                if all(w.confidence >= EXACT for w in found) and score <= 0.02 * total:
                    return found                  # the face drew this line at this size: nothing to improve
    return best[1] if best is not None else []


def _read_small(image: np.ndarray, box: tuple[int, int, int, int], font: Font) -> list[OcrWord]:
    """Choice 8: a line shorter than the synth font's cap height, scaled up to it and read with the synth font at
    `SMALL_QUALITY`; the box and cap height are mapped back to frame pixels."""
    bx, by, bw, bh = box
    alpha, ink = _band(image, bx - 1, by - 1, bx + bw + 1, by + bh + 1)
    rows, cols = np.flatnonzero(alpha.max(axis=1) > 0.2), np.flatnonzero(alpha.max(axis=0) > 0.2)
    if rows.size < 2 or cols.size < 2:
        return []
    crop = alpha[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
    ox, oy = bx - 1 + int(cols[0]), by - 1 + int(rows[0])
    bank = _bank(font)
    best: tuple[float, list[OcrWord]] | None = None
    for target in (bank.cap, bank.cap + 1, bank.cap + 2):
        f = target / crop.shape[0]
        up = np.clip(cv2.resize(crop, (max(1, round(crop.shape[1] * f)), target), interpolation=cv2.INTER_CUBIC), 0, 1)
        for drop in range(0, bank.descent + 1):            # rows of the line below the baseline
            top = bank.ascent - target + drop
            if top < 0 or top + target > bank.ascent + bank.descent + 1:
                continue
            band = np.zeros((bank.ascent + bank.descent + 1, up.shape[1] + 6), np.float32)
            band[top:top + target, 3:3 + up.shape[1]] = up
            words = _words_of(_decode(band, bank), band, bank, SMALL_QUALITY, 100.0 * SMALL_QUALITY)
            score = float(np.square(band).sum()) - sum(e - err for _, _, err, e in words) / 2
            found = []
            for text, pen, err, energy in words:
                (tw, th), base = cv2.getTextSize(text, font.face, font.scale, 1)
                baseline = oy + (bank.ascent - top) / f
                found.append(OcrWord(text, (ox + int(round((pen - 3) / f)), int(round(baseline - (th + 1) / f)),
                                            max(1, int(round(tw / f))), max(1, int(round((th + base + 1) / f)))),
                                     round(100.0 * max(0.0, 1.0 - err / energy), 1), round(th / f, 1),
                                     int(round(baseline)), tuple(int(v) for v in ink.round())))
            if found and (best is None or score < best[0] - 1e-6):
                best = (score, found)
    return best[1] if best is not None else []


def _foreign(height: int, font: Font) -> list[Font]:
    """The face at the scales whose cap height fits a blob of ink height `height` (choice 3): no descender, then
    descenders up to 40 percent of the height, both stroke types."""
    caps = range(height, height - max(4, round(0.4 * height)), -1)
    return [Font(font.face, s, lt) for cap in caps for lt in (cv2.LINE_8, cv2.LINE_AA)
            for s, c in _cap_table(font.face, lt) if c == cap]


def read_words(image: np.ndarray, font: Font = SYNTH_FONT,
               rects: Sequence[tuple[int, int, int, int]] | None = None) -> list[OcrWord]:
    """Every word of an RGB frame on a line that touches one of `rects` (x, y, w, h each; default the whole frame),
    read from its pixels once (module docstring), top to bottom then left to right."""
    if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
        raise ValueError("read_words reads an 8-bit RGB frame")
    H, W = image.shape[:2]
    rects = list(rects) if rects is not None else [(0, 0, W, H)]
    if not rects:
        return []
    ux0, uy0 = min(r[0] for r in rects), min(r[1] for r in rects)
    ux1, uy1 = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
    ink, ox, oy = _ink_mask(image, (ux0, uy0, ux1 - ux0, uy1 - uy0))
    bank = _bank(font)
    out: list[OcrWord] = []
    for bx, by, bw, bh in _blobs(ink):
        fx, fy = bx + ox, by + oy
        if not any(fx < rx + rw and fy < ry + rh and fx + bw > rx and fy + bh > ry for rx, ry, rw, rh in rects):
            continue                                   # a line that touches no rect
        if ink[by:by + bh, bx:bx + bw].mean() > SOLID or _thick(image[fy:fy + bh, fx:fx + bw]):
            continue                                   # a filled shape, not strokes of text
        box = (fx, fy, bw, bh)
        if bh < bank.cap - 1:
            if bh >= SMALL_MIN_H and bw >= 2 * bh:                     # a squeezed line, not a speck or a corner
                out += [w for w in _read_small(image, box, font) if sum(c.isalnum() for c in w.text) >= 2]
            continue
        native = bank.cap - 1 <= bh <= bank.ascent + bank.descent + 1
        first, second = ([font], _foreign(bh, font)) if native else (_foreign(bh, font), [font])
        out += _read_blob(image, box, first, native) or _read_blob(image, box, second, not native)
    return sorted(out, key=lambda w: (w.baseline, w.bbox[0]))
