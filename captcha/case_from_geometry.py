"""Recover captcha label case from glyph geometry.

An OCR model that was trained case-insensitively (ddddocr) cannot report the
case that was drawn -- it normalises every image to a fixed height, which is
precisely the signal that separates ``C`` from ``c``. The glyph *identities*
are already solid (the CRNN reads them correctly), so this module keeps those
and decides case from the shape of each glyph instead.

Three groups of letters, by how case shows up in this font:

* x-height (``acemnorsuvwxz``) -- the lowercase form is visibly shorter, so a
  height ratio decides it outright. These are also the same-shape pairs that an
  OCR model gets wrong most often.
* descender (``gjpqy``) -- both forms are tall, but only the lowercase one
  reaches below the baseline.
* ascender (``bdfhklt``) -- both forms are tall and neither descends, so
  geometry cannot separate them; the caller's existing guess is kept and the
  position is reported as uncertain.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np

XHEIGHT = set("acemnorsuvwxz")
DESCENDER = set("gjpqy")
ASCENDER = set("bdfhklt")

# Height ratio below which a glyph is x-height rather than cap height. Measured
# against the MEDIAN glyph height, not the tallest: the wave distortion stretches
# individual glyphs, and an ascender or a stretched letter is taller than a
# capital, which drags every capital below a max-based threshold. Against the
# median, x-height glyphs land at 0.71-0.77 and everything else at 0.95-1.05.
SHORT_RATIO = 0.86
# How far below the fitted baseline a glyph must reach to count as a descender,
# as a fraction of the median glyph height. Measured: lowercase 0.21-0.43,
# uppercase -0.05-0.10.
# Pairs that height cannot separate, decided from the shape of the ink instead.
# Each entry is (feature, op, threshold) and passing the test means the capital.
# Fitted on glyphs cut out of real captchas and read by eye -- 26 to 35 samples
# per pair -- then compared against what the CRNN says for the same glyphs:
#
#   letter  feature     rule        why
#   b       top_band    >= 0.42     lowercase: only a thin stem up top
#   d       top_band    >= 0.43     lowercase: only a thin stem up top
#   q       bot_band    >= 0.40     lowercase: only a thin tail below
#   g       density     <= 0.411    G is an open ring, g a filled bowl
#   y       density     <= 0.280    y's tail leaves more of the box empty
#
# Each rule earns its place by beating the label it overwrites, checked by eye
# on training images rather than on the captchas it was fitted to: b/d/q 10/10,
# y 8/9, g 2/2.
#
# Deliberately absent, having failed that same check:
#   f  cx          2/5   h  left_mass   2/3
#   j  dot+height  1/6   t  w_over_h    1/2
# They had measured 88-97% on glyphs cut out of fetched captchas, but that
# sample had the touching and clipped boxes filtered out for legibility, so it
# was the easy half of the data and the thresholds sat inside the real overlap.
# K/k never separated (76%). P/p cannot: the two are the same shape, a stem with
# a bowl, differing only in where they sit against a baseline this cannot see.
CASE_RULES = {
    "b": ("top_band", ">=", 0.42),
    "d": ("top_band", ">=", 0.43),
    "q": ("bot_band", ">=", 0.40),
    "g": ("density", "<=", 0.411),
    "y": ("density", "<=", 0.280),
}

# Letters this captcha only ever draws in one case -- the generator drops the
# confusable glyphs, so there is no lowercase l and no uppercase I (nor any
# o/O/0/1 at all). Nothing to measure: the alphabet settles it. Checked on 40
# hand-read captchas (L=6 l=0, I=0 i=4), 150 model readings, 12 cut-out glyphs
# that were all L, and 10 labelled images whose "l" was an L every time.
# Worth fixing rather than ignoring: ddddocr wrote 315 of these as lowercase l,
# which sits just under the frequency threshold, so core mode was dropping
# every one of those images out of the training set.
FIXED_CASE = {"l": "L", "i": "i"}
# x-height is about 0.72 of cap height, so the remaining 28% is the band where
# an ascender carries only its stem.
BAND = 0.28
# A glyph much shorter than its neighbours is a segmentation failure (a fragment
# of a letter), not a short letter; measuring it gives nonsense.
MIN_SIBLING_RATIO = 0.5

# Absolute fallback, in pixels, for images that contain no glyph of known cap
# height. Measured over 40 hand-read captchas: x-height lowercase spans 13-21px
# (median 16), cap height 19-28px (median 21), and 18 splits them at 97.7% --
# the same accuracy as the ratio, without needing a reference inside the image.
SHORT_PX = 18
MIN_AREA = 12


@dataclass
class GlyphGeom:
    x: int
    top: int
    bottom: int
    height: int
    width: int = 0
    rel_height: float = 0.0
    rel_descent: float = 0.0


@dataclass
class CaseResult:
    text: str
    uncertain: List[int] = field(default_factory=list)   # indexes geometry could not call
    glyphs: List[GlyphGeom] = field(default_factory=list)
    segmented: bool = True      # False when the glyph count could not be matched

    @property
    def confident(self) -> bool:
        return self.segmented and not self.uncertain


def text_mask(path: Path, close: bool = True) -> np.ndarray:
    """Binary mask of the red lettering, with the grey speckles dropped.

    Thresholding luminance would keep the background specks, which are as dark
    as the glyphs; redness separates them because the specks are neutral.
    ``close=False`` skips the closing step, which is what the dot on a ``j``
    needs: closing bridges the gap and merges it into the stem.
    """
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise FileNotFoundError(path)
    b, g, r = cv2.split(bgr.astype(np.int16))
    redness = np.clip(r - np.maximum(g, b), 0, 255).astype(np.uint8)
    if int(redness.max()) < 30:          # not red text: fall back to luminance
        grey = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        redness = 255 - grey
    _, mask = cv2.threshold(redness, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if not close:
        return mask
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))


def _components(mask: np.ndarray) -> List[Tuple[int, int, int, int]]:
    n, _lab, stats, _c = cv2.connectedComponentsWithStats(mask, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = stats[i]
        if a >= MIN_AREA and h >= 6:
            out.append((int(x), int(y), int(w), int(h)))
    out.sort(key=lambda c: c[0])
    return out


def _split_widest(boxes: List[Tuple[int, int, int, int]], mask: np.ndarray
                  ) -> List[Tuple[int, int, int, int]]:
    """Cut the widest box at its thinnest ink column, so touching glyphs separate."""
    i = max(range(len(boxes)), key=lambda k: boxes[k][2])
    x, y, w, h = boxes[i]
    if w < 10:
        return boxes
    col = (mask[y:y + h, x:x + w] > 0).sum(axis=0)
    lo, hi = int(w * 0.3), int(w * 0.7)
    cut = lo + int(np.argmin(col[lo:hi])) if hi > lo else w // 2
    left = mask[y:y + h, x:x + cut]
    right = mask[y:y + h, x + cut:x + w]

    def shrink(sub, x0):
        ys, xs = np.nonzero(sub)
        if len(ys) == 0:
            return None
        return (x0 + int(xs.min()), y + int(ys.min()),
                int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))

    parts = [p for p in (shrink(left, x), shrink(right, x + cut)) if p]
    if len(parts) != 2:
        return boxes
    return sorted(boxes[:i] + parts + boxes[i + 1:], key=lambda c: c[0])


def glyph_geometry(path: Path, want: int) -> Tuple[List[GlyphGeom], bool]:
    mask = text_mask(path)
    boxes = _components(mask)
    guard = 0
    while len(boxes) < want and guard < want * 2:
        new = _split_widest(boxes, mask)
        if len(new) == len(boxes):
            break
        boxes = new
        guard += 1
    if len(boxes) > want:                       # drop the smallest leftovers (noise)
        boxes = sorted(sorted(boxes, key=lambda c: c[2] * c[3],
                              reverse=True)[:want], key=lambda c: c[0])
    matched = len(boxes) == want
    if not boxes:
        return [], False

    glyphs = [GlyphGeom(x=x, top=y, bottom=y + h, height=h, width=w)
              for x, y, w, h in boxes]
    return glyphs, matched


def _cap_height(glyphs: Sequence[GlyphGeom], text: str) -> Optional[float]:
    """Cap height from the glyphs that have it whatever their case.

    Digits, ascender letters (``bdfhklt``) and descender letters (``gjpqy``) all
    reach the cap line in both cases, so their identity alone makes them a safe
    reference; only the x-height letters are in question. Any statistic over
    *all* glyphs fails instead on the mixture: a median lands between the two
    clusters when half the glyphs are short, and a 75th percentile lands inside
    the short cluster when five of six are (``vEneaa`` came out ``VENEAA``).

    Returns ``None`` when the word is made entirely of x-height letters, so
    there is no reference and the caller must use the absolute threshold.
    """
    hs = [float(g.height) for ch, g in zip(text, glyphs)
          if ch.isdigit() or ch.lower() in ASCENDER or ch.lower() in DESCENDER]
    return float(np.median(hs)) if hs else None


def _glyph_ink(mask: np.ndarray, g: GlyphGeom) -> Optional[Dict[str, float]]:
    """Shape measurements for one glyph, all scale-free."""
    sub = mask[g.top:g.bottom, g.x:g.x + g.width] > 0
    if sub.size == 0 or sub.shape[0] < 4:
        return None
    h, w = sub.shape
    rows = sub.sum(axis=1).astype(float)
    cols = sub.sum(axis=0).astype(float)
    widest = rows.max()
    total = cols.sum()
    if widest <= 0 or total <= 0:
        return None
    k = max(1, int(round(h * BAND)))
    xs = np.nonzero(sub)[1]
    return {
        "top_band": float(rows[:k].mean() / widest),
        "bot_band": float(rows[-k:].mean() / widest),
        "density": float(sub.mean()),
        "left_mass": float(cols[:w // 2].sum() / total),
        "w_over_h": float(w / h),
        "cx": float(xs.mean() / w),
    }


def apply_case(text: str, path: Path) -> CaseResult:
    """Re-case ``text`` (identities trusted, case ignored) from the image."""
    glyphs, matched = glyph_geometry(path, len(text))
    if not matched:
        return CaseResult(text=text, uncertain=list(range(len(text))),
                          glyphs=glyphs, segmented=False)

    cap = _cap_height(glyphs, text)
    for g in glyphs:
        g.rel_height = g.height / cap if cap else 0.0
    tallest = max(g.height for g in glyphs)
    mask = text_mask(path)

    out, uncertain = [], []
    for i, (ch, g) in enumerate(zip(text, glyphs)):
        low = ch.lower()
        if g.height < tallest * MIN_SIBLING_RATIO:
            # A fragment, not a letter: every measurement below would be noise.
            out.append(ch)
            uncertain.append(i)
        elif not ch.isalpha():
            out.append(ch)
        elif low in FIXED_CASE:
            out.append(FIXED_CASE[low])
        elif low in XHEIGHT:
            # Size is the only thing separating these pairs, and it separates
            # them cleanly, so geometry decides and the CRNN never has to.
            short = (g.rel_height < SHORT_RATIO) if cap else (g.height < SHORT_PX)
            out.append(low if short else low.upper())
        elif low in CASE_RULES:
            name, op, thresh = CASE_RULES[low]
            ink = _glyph_ink(mask, g)
            if ink is None:
                out.append(ch)
                uncertain.append(i)
            else:
                hit = ink[name] >= thresh if op == ">=" else ink[name] <= thresh
                out.append(low.upper() if hit else low)
        else:
            # Ascender and descender pairs are cap-tall in both cases. Descent
            # would separate the descenders, but it is not measurable: the
            # baseline can only be fitted where glyphs anchor it, and the first
            # and last glyph fall outside that span, where extrapolating the
            # wave costs more pixels than the descent being measured. Guessing
            # anyway scored 74.5% and broke more labels than it fixed, so leave
            # these alone -- they differ in shape, which is the CRNN's job --
            # and flag the position.
            out.append(ch)
            uncertain.append(i)
    return CaseResult(text="".join(out), uncertain=uncertain, glyphs=glyphs)
