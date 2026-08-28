"""Bbox conversion is the highest-risk normalization surface. These tests pin the canonical
convention (top-left, y-down, [0,1]) against the coordinate facts verified empirically in
internal/research/openreading/_data/live_runs.md (US-letter 612x792, the page-1 title glyph):

  - pdfminer / pdfplumber y0/y1 : bottom-left, y-up   -> y0=707.86, y1=727.86
  - pdfplumber top/bottom       : top-left,   y-down  -> top=64.14, bottom=84.14
  - PyMuPDF span bbox           : top-left,   y-down, ascender-inflated -> top=58.5, bottom=85.98
  - pytesseract                 : top-left,   y-down, dpi-dependent pixels

Cross-library point: the two TIGHT boxes (pdfminer y-up and pdfplumber y-down) describe the
SAME glyph and must land on the SAME canonical box; the ascender-inflated PyMuPDF box must
NOT be forced equal to them — we preserve the difference rather than fabricate agreement.
"""

from __future__ import annotations

import math

from openreading.types import (
    NativeOrigin,
    NativeUnit,
    pixels_from_points,
    to_absolute,
    to_canonical,
)

PAGE_W, PAGE_H = 612.0, 792.0  # US-letter points
X0, X1 = 72.0, 200.0  # shared horizontal extent (1-inch left margin)


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return math.isclose(a, b, abs_tol=tol)


def test_bottom_left_and_top_left_agree_for_same_glyph():
    # pdfminer / pdfplumber y0,y1 in bottom-left y-up space
    canon_bl = to_canonical(
        [X0, 707.86, X1, 727.86],
        origin=NativeOrigin.BOTTOM_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=PAGE_W,
        page_height=PAGE_H,
        page=1,
    )
    # pdfplumber top,bottom in top-left y-down space (same glyph)
    canon_tl = to_canonical(
        [X0, 64.14, X1, 84.14],
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=PAGE_W,
        page_height=PAGE_H,
        page=1,
    )
    assert _close(canon_bl.x, canon_tl.x)
    assert _close(canon_bl.y, canon_tl.y, tol=1e-4)
    assert _close(canon_bl.w, canon_tl.w)
    assert _close(canon_bl.h, canon_tl.h, tol=1e-4)
    # and the numbers are what the live run implies
    assert _close(canon_tl.y, 64.14 / PAGE_H, tol=1e-6)
    assert _close(canon_tl.h, 20.0 / PAGE_H, tol=1e-4)


def test_pymupdf_ascender_inflation_is_preserved_not_flattened():
    tight = to_canonical(
        [X0, 64.14, X1, 84.14],
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=PAGE_W,
        page_height=PAGE_H,
        page=1,
    )
    inflated = to_canonical(
        [X0, 58.5, X1, 85.98],  # PyMuPDF span box: ascender/descender inflated
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=PAGE_W,
        page_height=PAGE_H,
        page=1,
    )
    assert inflated.h > tight.h  # taller: the inflation is retained
    assert inflated.y < tight.y  # starts higher on the page
    # raw geometry is preserved verbatim for auditing
    assert inflated.bbox_native.coords == [X0, 58.5, X1, 85.98]
    assert inflated.bbox_native.origin is NativeOrigin.TOP_LEFT


def test_roundtrip_bottom_left_recovers_source():
    src = [X0, 707.86, X1, 727.86]
    canon = to_canonical(
        src,
        origin=NativeOrigin.BOTTOM_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=PAGE_W,
        page_height=PAGE_H,
        page=1,
    )
    x0, y0, x1, y1 = to_absolute(
        canon, page_width=PAGE_W, page_height=PAGE_H, origin=NativeOrigin.BOTTOM_LEFT
    )
    assert _close(x0, src[0], tol=1e-3)
    assert _close(y0, src[1], tol=1e-3)
    assert _close(x1, src[2], tol=1e-3)
    assert _close(y1, src[3], tol=1e-3)


def test_roundtrip_top_left_recovers_source():
    src = [X0, 64.14, X1, 84.14]
    canon = to_canonical(
        src,
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=PAGE_W,
        page_height=PAGE_H,
        page=1,
    )
    got = to_absolute(canon, page_width=PAGE_W, page_height=PAGE_H, origin=NativeOrigin.TOP_LEFT)
    for a, b in zip(got, src, strict=True):
        assert _close(a, b, tol=1e-3)


def test_pixel_dpi_conversion_normalizes_and_roundtrips():
    dpi = 150.0
    page_w_px = pixels_from_points(PAGE_W, dpi)  # 1275
    page_h_px = pixels_from_points(PAGE_H, dpi)  # 1650
    assert _close(page_w_px, 1275.0) and _close(page_h_px, 1650.0)
    # a top-left pixel box at the same tight-glyph top edge (64.14pt -> 133.6px)
    top_px = pixels_from_points(64.14, dpi)
    bot_px = pixels_from_points(84.14, dpi)
    canon = to_canonical(
        [pixels_from_points(X0, dpi), top_px, pixels_from_points(X1, dpi), bot_px],
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.PIXEL,
        page_width=page_w_px,
        page_height=page_h_px,
        page=1,
        dpi=dpi,
    )
    # pixel-derived canonical box matches the point-derived one for the same glyph
    assert _close(canon.y, 64.14 / PAGE_H, tol=1e-4)
    assert _close(canon.h, 20.0 / PAGE_H, tol=1e-4)
    assert canon.bbox_native.unit is NativeUnit.PIXEL
    assert canon.bbox_native.dpi == dpi


def test_normalized_passthrough_is_identity():
    # Textract-style: already normalized [0,1] top-left {Left,Top,Width,Height} -> xyxy
    canon = to_canonical(
        [0.1, 0.2, 0.5, 0.35],
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.NORMALIZED,
        page_width=1.0,
        page_height=1.0,
        page=3,
    )
    assert _close(canon.x, 0.1) and _close(canon.y, 0.2)
    assert _close(canon.w, 0.4) and _close(canon.h, 0.15)
    assert canon.page == 3


def test_all_canonical_values_stay_in_unit_interval():
    # a box flush to the page edges must not overflow [0,1] after clamping
    canon = to_canonical(
        [0.0, 0.0, PAGE_W, PAGE_H],
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=PAGE_W,
        page_height=PAGE_H,
        page=1,
    )
    for v in (canon.x, canon.y, canon.w, canon.h):
        assert 0.0 <= v <= 1.0
