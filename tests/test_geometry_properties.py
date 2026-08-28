"""Property/invariant tests for the bbox-conversion choke point (types/geometry.py) — the
codebase's self-described highest-risk normalization surface. The existing test_geometry.py pins
specific empirically-verified glyphs; this file asserts the *invariants* that must hold for ANY
input, across many seeded-random cases (a fixed seed keeps failures reproducible — no hypothesis
dependency, in keeping with the project's dependency discipline)."""

from __future__ import annotations

import random

import pytest

from openreading.types import NativeOrigin, NativeUnit, to_absolute, to_canonical
from openreading.types.geometry import bbox_from_polygon

_ORIGINS = [NativeOrigin.TOP_LEFT, NativeOrigin.BOTTOM_LEFT]
_UNITS = [NativeUnit.PDF_POINT, NativeUnit.PIXEL, NativeUnit.INCH, NativeUnit.NORMALIZED]
_N = 400  # cases per invariant


def _rng(tag: str) -> random.Random:
    # deterministic per-invariant stream; a failing case is reproducible from (tag, index)
    return random.Random(f"openreading-geometry::{tag}")


def _rand_page(rng: random.Random) -> tuple[float, float]:
    return rng.uniform(1.0, 5000.0), rng.uniform(1.0, 5000.0)


def _rand_box(rng: random.Random, pw: float, ph: float) -> list[float]:
    # deliberately unordered and sometimes out of bounds — the converter must cope
    return [
        rng.uniform(-pw * 0.2, pw * 1.2),
        rng.uniform(-ph * 0.2, ph * 1.2),
        rng.uniform(-pw * 0.2, pw * 1.2),
        rng.uniform(-ph * 0.2, ph * 1.2),
    ]


# ---- core invariant: canonical output is always a schema-valid, in-page box --------------------


def test_canonical_values_always_in_unit_interval_and_inside_page():
    rng = _rng("in-interval")
    for _ in range(_N):
        pw, ph = _rand_page(rng)
        coords = _rand_box(rng, pw, ph)
        origin = rng.choice(_ORIGINS)
        page = rng.randint(1, 999)
        # BBox fields are Field(ge=0, le=1) — if any canonical value escaped [0,1] this raises.
        b = to_canonical(
            coords, origin=origin, unit=rng.choice(_UNITS), page_width=pw, page_height=ph, page=page
        )
        for v in (b.x, b.y, b.w, b.h):
            assert 0.0 <= v <= 1.0
        # the box never pokes out past the page's right/bottom edge after clamping
        assert b.x + b.w <= 1.0 + 1e-9
        assert b.y + b.h <= 1.0 + 1e-9
        assert b.page == page


def test_native_geometry_preserves_the_raw_coords_verbatim():
    rng = _rng("native")
    for _ in range(_N):
        pw, ph = _rand_page(rng)
        coords = _rand_box(rng, pw, ph)
        origin, unit = rng.choice(_ORIGINS), rng.choice(_UNITS)
        b = to_canonical(
            coords, origin=origin, unit=unit, page_width=pw, page_height=ph, page=1, dpi=150.0
        )
        # lossless audit trail: raw coords, origin, unit, dpi are stored untouched (original order)
        assert b.bbox_native.coords == [float(c) for c in coords]
        assert b.bbox_native.origin is origin and b.bbox_native.unit is unit
        assert b.bbox_native.dpi == 150.0


def test_corner_order_does_not_change_the_canonical_box():
    # the converter min/max-normalizes corners, so swapping x0<->x1 and y0<->y1 is a no-op
    rng = _rng("corner-order")
    for _ in range(_N):
        pw, ph = _rand_page(rng)
        x0, y0, x1, y1 = _rand_box(rng, pw, ph)
        origin = rng.choice(_ORIGINS)
        kw = dict(origin=origin, unit=NativeUnit.PDF_POINT, page_width=pw, page_height=ph, page=1)
        a = to_canonical([x0, y0, x1, y1], **kw)
        b = to_canonical([x1, y1, x0, y0], **kw)
        assert (a.x, a.y, a.w, a.h) == pytest.approx((b.x, b.y, b.w, b.h))


# ---- round-trip: to_absolute inverts to_canonical for in-bounds boxes --------------------------


def _rand_inbounds(rng: random.Random, span: float) -> tuple[float, float]:
    lo = rng.uniform(0.0, span)
    hi = rng.uniform(0.0, span)
    return (min(lo, hi), max(lo, hi))


@pytest.mark.parametrize("origin", _ORIGINS)
def test_absolute_recovers_source_for_inbounds_boxes(origin):
    rng = _rng(f"roundtrip-{origin.value}")
    for _ in range(_N):
        pw, ph = _rand_page(rng)
        left, right = _rand_inbounds(rng, pw)
        lo, hi = _rand_inbounds(rng, ph)
        # feed coordinates in the source convention: y-up for BOTTOM_LEFT, y-down for TOP_LEFT
        src = [left, lo, right, hi]
        canon = to_canonical(
            src, origin=origin, unit=NativeUnit.PDF_POINT, page_width=pw, page_height=ph, page=1
        )
        gx0, gy0, gx1, gy1 = to_absolute(canon, page_width=pw, page_height=ph, origin=origin)
        # tolerance scales with page size (float division/reconstruction)
        tol = max(pw, ph) * 1e-6
        assert gx0 == pytest.approx(left, abs=tol)
        assert gx1 == pytest.approx(right, abs=tol)
        assert gy0 == pytest.approx(lo, abs=tol)
        assert gy1 == pytest.approx(hi, abs=tol)


# ---- polygon → bbox invariants ----------------------------------------------------------------


def test_polygon_bbox_covers_all_points_and_stays_normalized():
    rng = _rng("polygon")
    for _ in range(_N):
        pw, ph = _rand_page(rng)
        pts = [[rng.uniform(0, pw), rng.uniform(0, ph)] for _ in range(rng.randint(4, 8))]
        b = bbox_from_polygon(
            pts,
            origin=NativeOrigin.TOP_LEFT,
            unit=NativeUnit.PIXEL,
            page_width=pw,
            page_height=ph,
            page=1,
        )
        # the derived box is normalized and encloses the polygon's own normalized extent
        assert 0.0 <= b.x <= 1.0 and 0.0 <= b.y <= 1.0
        assert b.polygon is not None and len(b.polygon) == len(pts)
        for px, py in b.polygon:
            assert 0.0 <= px <= 1.0 and 0.0 <= py <= 1.0
            assert b.x - 1e-9 <= px <= b.x + b.w + 1e-9
            assert b.y - 1e-9 <= py <= b.y + b.h + 1e-9


# ---- input-validation guards (the two remaining uncovered lines) ------------------------------


@pytest.mark.parametrize("coords", [[], [1.0], [1.0, 2.0, 3.0], [1, 2, 3, 4, 5]])
def test_wrong_coordinate_count_raises(coords):
    with pytest.raises(ValueError, match="x0,y0,x1,y1"):
        to_canonical(
            coords,
            origin=NativeOrigin.TOP_LEFT,
            unit=NativeUnit.PDF_POINT,
            page_width=100.0,
            page_height=100.0,
            page=1,
        )


@pytest.mark.parametrize("pw,ph", [(0.0, 100.0), (100.0, 0.0), (-1.0, 100.0), (100.0, -5.0)])
def test_nonpositive_page_dimensions_raise(pw, ph):
    with pytest.raises(ValueError, match="page dims must be positive"):
        to_canonical(
            [0, 0, 10, 10],
            origin=NativeOrigin.TOP_LEFT,
            unit=NativeUnit.PDF_POINT,
            page_width=pw,
            page_height=ph,
            page=1,
        )
