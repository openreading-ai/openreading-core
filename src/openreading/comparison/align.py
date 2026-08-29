"""Alignment engine (DESIGN §5 — the technical heart). Backends disagree on *segmentation*, not
just content: one's paragraph is another's three lines. `align_pair` matches blocks text-first,
validates with canonical-bbox IoU when present, and handles granularity by letting a merged run of
adjacent same-type blocks on the finer side match a single coarser block. Deterministic: candidate
pairs are scored, then accepted greedily by (score, IoU, index) — no randomness, no deps beyond
stdlib. Thresholds are documented constants here, never flags (DESIGN §5). Calibrating them
against adjudicated corpora is company-repo work (E5), not a knob this package exposes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

TAU_TEXT = 0.60  # minimum token similarity to call two blocks a match
IOU_MIN = 0.30  # a text match with same-page IoU below this is demoted to position_conflict
MERGE_LOOKAHEAD = 4  # max adjacent same-type blocks concatenated on the finer side
METHOD = "text_first/v1"

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_block_text(s: str) -> str:
    """Casefold, drop punctuation, collapse whitespace — the matching key for block text."""
    return _WS.sub(" ", _PUNCT.sub(" ", s.casefold())).strip()


def token_similarity(a: str, b: str) -> float:
    ta, tb = a.split(), b.split()
    if not ta and not tb:
        return 1.0
    return SequenceMatcher(None, ta, tb).ratio()


def block_bbox(block: dict[str, Any]) -> tuple[float, float, float, float] | None:
    bb = block.get("bbox")
    if not isinstance(bb, dict):
        return None
    try:
        return (float(bb["x"]), float(bb["y"]), float(bb["w"]), float(bb["h"]))
    except (KeyError, TypeError, ValueError):
        return None


def _union_bbox(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return (x0, y0, x1 - x0, y1 - y0)


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2 = min(a[0] + a[2], b[0] + b[2])
    iy2 = min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


@dataclass(frozen=True)
class Unit:
    """A candidate match unit: one block, or a merged run of adjacent same-type blocks."""

    indices: tuple[int, ...]
    text: str  # normalized, non-empty
    btype: str
    bbox: tuple[float, float, float, float] | None


def _units(blocks: list[dict[str, Any]]) -> list[Unit]:
    units: list[Unit] = []
    n = len(blocks)
    for i in range(n):
        boxes: list[tuple[float, float, float, float]] = []
        all_boxed = True
        for length in range(1, MERGE_LOOKAHEAD + 1):
            if i + length > n:
                break
            span = blocks[i : i + length]
            types = {b.get("type") for b in span}
            if length > 1 and len(types) != 1:
                break  # stop extending a run once the block type changes
            text = normalize_block_text(" ".join(b.get("text") or "" for b in span))
            bb = block_bbox(blocks[i + length - 1])
            if bb is None:
                all_boxed = False
            else:
                boxes.append(bb)
            if not text:
                continue  # empty text is never a meaningful match unit
            single = next(iter(types)) if len(types) == 1 else None
            units.append(
                Unit(
                    indices=tuple(range(i, i + length)),
                    text=text,
                    btype=str(single) if single is not None else "mixed",
                    bbox=_union_bbox(boxes) if all_boxed and boxes else None,
                )
            )
    return units


@dataclass(frozen=True)
class Match:
    a: Unit
    b: Unit
    score: float
    iou: float | None  # None when either side lacks a bbox


def align_pair(
    blocks_a: list[dict[str, Any]], blocks_b: list[dict[str, Any]]
) -> tuple[list[Match], list[int], list[int]]:
    """Align two blocks-lists. Returns (matches, unmatched_a_indices, unmatched_b_indices).
    Granularity merge is implicit: merged runs are candidate units, so a 3-line side can match a
    1-paragraph side when the concatenation scores higher than any single line."""
    units_a, units_b = _units(blocks_a), _units(blocks_b)
    candidates: list[tuple[float, float, Unit, Unit]] = []
    for ua in units_a:
        for ub in units_b:
            s = token_similarity(ua.text, ub.text)
            if s >= TAU_TEXT:
                io = iou(ua.bbox, ub.bbox) if ua.bbox and ub.bbox else -1.0
                candidates.append((s, io, ua, ub))
    # Highest score first; IoU breaks score ties; index order is the final deterministic tie-break.
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2].indices, c[3].indices))

    used_a: set[int] = set()
    used_b: set[int] = set()
    matches: list[Match] = []
    for s, io, ua, ub in candidates:
        if used_a.intersection(ua.indices) or used_b.intersection(ub.indices):
            continue
        used_a.update(ua.indices)
        used_b.update(ub.indices)
        matches.append(Match(ua, ub, s, io if io >= 0 else None))

    unmatched_a = [i for i in range(len(blocks_a)) if i not in used_a]
    unmatched_b = [i for i in range(len(blocks_b)) if i not in used_b]
    return matches, unmatched_a, unmatched_b
