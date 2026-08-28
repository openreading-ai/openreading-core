"""Canonical geometry + the single bbox-conversion choke point (DECISIONS D6).

Canonical convention (normalized_schema.md §6.1): top-left origin, y increases downward,
normalized to [0,1] of the page's width/height. Every adapter routes its raw geometry
through `to_canonical()`, which also records the untouched source geometry in `bbox_native`
so the conversion is lossless and auditable.

The coordinate facts here were verified empirically against the same glyph across libraries
(internal/research/openreading/_data/live_runs.md, 2026-07-21): PyMuPDF reports an ascender-inflated top-left/y-down box,
pdfminer reports a tight bottom-left/y-up box, pdfplumber carries BOTH conventions, and
pytesseract reports dpi-dependent top-left pixels. There is no global flip that fixes all
of them — the source origin/unit must travel with the coordinates.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from openreading.types.enums import NativeOrigin, NativeUnit


class NativeGeometry(BaseModel):
    """Raw source geometry, untouched, for round-trip fidelity (response schema BBox.bbox_native)."""

    model_config = ConfigDict(extra="forbid")

    coords: list[float]
    origin: NativeOrigin
    unit: NativeUnit
    dpi: float | None = None


class BBox(BaseModel):
    """CANONICAL geometry emitted by the normalizer (response schema $defs.BBox)."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(ge=0.0, le=1.0)
    h: float = Field(ge=0.0, le=1.0)
    page: int = Field(ge=1)
    polygon: list[list[float]] | None = None
    bbox_native: NativeGeometry | None = None


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def pixels_from_points(points: float, dpi: float) -> float:
    """Convert a PDF-point length to pixels at a rasterization dpi (72 pt = 1 inch)."""
    return points / 72.0 * dpi


def to_canonical(
    coords: Sequence[float],
    *,
    origin: NativeOrigin,
    unit: NativeUnit,
    page_width: float,
    page_height: float,
    page: int,
    dpi: float | None = None,
    polygon: Sequence[Sequence[float]] | None = None,
) -> BBox:
    """Convert a source [x0, y0, x1, y1] box into the canonical normalized top-left BBox.

    `page_width`/`page_height` are in the SAME native unit as `coords`:
    points for pdf_point, pixels for pixel, inches for inch, and 1.0/1.0 for normalized
    (i.e. the coords are already fractions of the page).

    The raw box is preserved verbatim in `bbox_native`; the canonical values are clamped
    to [0,1] to satisfy the schema (clamping is safe because the raw form is retained).
    """
    if len(coords) != 4:
        raise ValueError(f"expected [x0,y0,x1,y1], got {list(coords)!r}")
    if page_width <= 0 or page_height <= 0:
        raise ValueError(f"page dims must be positive, got {page_width}x{page_height}")

    x0, y0, x1, y1 = (float(c) for c in coords)
    left, right = min(x0, x1), max(x0, x1)
    lo, hi = min(y0, y1), max(y0, y1)

    # Fold the vertical axis into top-left/y-down space: for bottom-left/y-up sources the
    # visually-top edge is the LARGER y value, so its top-down distance is page_height - hi.
    top = (page_height - hi) if origin is NativeOrigin.BOTTOM_LEFT else lo

    x = _clamp01(left / page_width)
    y = _clamp01(top / page_height)
    w = _clamp01((right - left) / page_width)
    h = _clamp01((hi - lo) / page_height)
    # keep the box inside the page after clamping the origin
    w = _clamp01(min(w, 1.0 - x))
    h = _clamp01(min(h, 1.0 - y))

    norm_polygon: list[list[float]] | None = None
    if polygon is not None:
        norm_polygon = []
        for pt in polygon:
            px, py = float(pt[0]), float(pt[1])
            py_top = (page_height - py) if origin is NativeOrigin.BOTTOM_LEFT else py
            norm_polygon.append([_clamp01(px / page_width), _clamp01(py_top / page_height)])

    return BBox(
        x=x,
        y=y,
        w=w,
        h=h,
        page=page,
        polygon=norm_polygon,
        bbox_native=NativeGeometry(
            coords=[x0, y0, x1, y1],
            origin=origin,
            unit=unit,
            dpi=dpi,
        ),
    )


def to_absolute(
    box: BBox,
    *,
    page_width: float,
    page_height: float,
    origin: NativeOrigin = NativeOrigin.TOP_LEFT,
) -> tuple[float, float, float, float]:
    """Recover an absolute [x0, y0, x1, y1] box in `page_width`/`page_height` units.

    Inverse of `to_canonical` for the top-left case; pass origin=BOTTOM_LEFT to recover
    y-up coordinates (used to round-trip against pdfminer-style sources in tests).
    """
    left = box.x * page_width
    right = (box.x + box.w) * page_width
    top = box.y * page_height
    bottom = (box.y + box.h) * page_height
    if origin is NativeOrigin.BOTTOM_LEFT:
        y_lo = page_height - bottom
        y_hi = page_height - top
        return (left, y_lo, right, y_hi)
    return (left, top, right, bottom)


def bbox_from_polygon(
    polygon: Sequence[Sequence[float]],
    *,
    origin: NativeOrigin,
    unit: NativeUnit,
    page_width: float,
    page_height: float,
    page: int,
    dpi: float | None = None,
) -> BBox:
    """Build a canonical BBox from a 4+ point polygon (Textract/DocAI/Azure/unstructured)."""
    xs = [float(p[0]) for p in polygon]
    ys = [float(p[1]) for p in polygon]
    return to_canonical(
        [min(xs), min(ys), max(xs), max(ys)],
        origin=origin,
        unit=unit,
        page_width=page_width,
        page_height=page_height,
        page=page,
        dpi=dpi,
        polygon=polygon,
    )
