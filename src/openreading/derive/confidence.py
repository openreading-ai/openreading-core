"""Confidence aggregation (DESIGN §5, §4.5)."""

from __future__ import annotations

from collections.abc import Sequence


def aggregate_confidence(word_confs: Sequence[float | None]) -> float | None:
    """Aggregate member confidences to an element confidence: the **min** (a usable quality
    floor — mean would hide one garbage word). None when there are no members."""
    confs = [c for c in word_confs if c is not None]
    return min(confs) if confs else None
