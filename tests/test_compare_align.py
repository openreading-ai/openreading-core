"""H2 — alignment torture table (DESIGN §5). Each case pins the pairwise aligner's behavior: merge,
lookahead cap, reordering, sub-threshold rejection, and the geometry helpers. A threshold change
that shifts behavior fails here, visibly."""

from __future__ import annotations

from openreading.comparison.align import (
    MERGE_LOOKAHEAD,
    align_pair,
    iou,
    normalize_block_text,
)


def blk(text: str, btype: str = "text") -> dict:
    return {"type": btype, "text": text}


def test_identical_single_match() -> None:
    matches, ua, ub = align_pair([blk("hello world")], [blk("hello world")])
    assert len(matches) == 1 and ua == [] and ub == []


def test_granularity_merge_three_lines_to_one_paragraph() -> None:
    a = [blk("the quick brown fox jumps over")]
    b = [blk("the quick"), blk("brown fox"), blk("jumps over")]
    matches, ua, ub = align_pair(a, b)
    assert len(matches) == 1
    assert ua == [] and ub == []
    assert matches[0].b.indices == (0, 1, 2)  # all three lines merged into one unit


def test_merge_capped_at_lookahead() -> None:
    # five single-token lines vs one five-token block; lookahead=4 cannot cover all five
    assert MERGE_LOOKAHEAD == 4
    a = [blk("alpha beta gamma delta epsilon")]
    b = [blk(t) for t in ("alpha", "beta", "gamma", "delta", "epsilon")]
    _matches, _ua, ub = align_pair(a, b)
    assert len(ub) >= 1  # at least one line is left unmatched


def test_reordered_reading_order_still_matches() -> None:
    a = [blk("alpha one"), blk("beta two")]
    b = [blk("beta two"), blk("alpha one")]
    matches, ua, ub = align_pair(a, b)
    assert len(matches) == 2 and ua == [] and ub == []


def test_below_tau_is_unmatched() -> None:
    matches, ua, ub = align_pair([blk("completely different")], [blk("nothing alike here")])
    assert matches == [] and ua == [0] and ub == [0]


def test_determinism() -> None:
    a = [blk("shared text one"), blk("shared text two")]
    b = [blk("shared text one"), blk("shared text two")]
    r1, r2 = align_pair(a, b), align_pair(a, b)
    key = lambda r: [(m.a.indices, m.b.indices) for m in r[0]]  # noqa: E731
    assert key(r1) == key(r2)


def test_helpers() -> None:
    assert normalize_block_text("Hello, WORLD!") == "hello world"
    assert iou((0, 0, 1, 1), (0, 0, 1, 1)) == 1.0
    assert iou((0, 0, 0.5, 1), (0.5, 0, 0.5, 1)) == 0.0
    assert 0.0 < iou((0, 0, 1, 1), (0.5, 0, 1, 1)) < 1.0
