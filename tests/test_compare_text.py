"""M2a — text delta (DESIGN §4C) + the one canonical-text derivation (shared with evals)."""

from __future__ import annotations

from openreading.comparison import compare
from openreading.evals.scorers import canonical_text
from tests.fakes import make_envelope


def _text(report: dict) -> dict:
    return report["text"]


# --- canonical text: the single derivation rule (text → markdown → blocks in reading order) ---


def test_canonical_prefers_markdown() -> None:
    resp = make_envelope("a", markdown="# Heading", text="plain fallback")
    assert canonical_text(resp) == "plain fallback"


def test_canonical_falls_back_to_text() -> None:
    resp = make_envelope("a", text="plain body")
    assert canonical_text(resp) == "plain body"


def test_canonical_falls_back_to_blocks_in_reading_order() -> None:
    resp = make_envelope(
        "a",
        pages=[
            [
                {"type": "text", "text": "second", "reading_order": 1},
                {"type": "text", "text": "first", "reading_order": 0},
            ]
        ],
    )
    assert canonical_text(resp) == "first\nsecond"


def test_canonical_empty() -> None:
    assert canonical_text(make_envelope("a", text="")) == ""


# --- pairwise matrix + pairs ------------------------------------------------------------


def test_matrix_is_square_with_unit_diagonal() -> None:
    subs = [make_envelope(x, text=f"body {x}") for x in ("a", "b", "c")]
    matrix = _text(compare(subs))["matrix"]
    assert len(matrix) == 3 and all(len(row) == 3 for row in matrix)
    assert all(matrix[i][i] == 1.0 for i in range(3))


def test_matrix_symmetric() -> None:
    a = make_envelope("a", text="the quick brown fox")
    b = make_envelope("b", text="the quick red fox")
    matrix = _text(compare([a, b]))["matrix"]
    assert matrix[0][1] == matrix[1][0]
    assert 0.0 < matrix[0][1] < 1.0


def test_pairs_carry_unique_char_counts() -> None:
    a = make_envelope("a", text="hello world")
    b = make_envelope("b", text="hello")
    pairs = _text(compare([a, b]))["pairs"]
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["a"] == "a" and pair["b"] == "b"
    assert pair["a_only_chars"] == len(" world")  # "hello" matches; " world" is a-only
    assert pair["b_only_chars"] == 0


def test_text_divergence_finding_when_far_apart() -> None:
    a = make_envelope("a", text="completely different content here")
    b = make_envelope("b", text="nothing alike whatsoever")
    report = compare([a, b])
    assert any(f["code"] == "text_divergence" for f in report["findings"])


def test_identical_text_no_divergence() -> None:
    a = make_envelope("a", text="same text")
    b = make_envelope("b", text="same text")
    report = compare([a, b])
    assert _text(report)["matrix"][0][1] == 1.0
    assert not any(f["code"] == "text_divergence" for f in report["findings"])


def test_identical_text_differing_markdown_no_divergence() -> None:
    """BL-127: byte-identical `document.text` must win over differing markdown styling. Two
    subjects saying the same thing in a different GFM dialect (pipe table/heading/plain emphasis
    vs. HTML table/bold/underscore emphasis) must never read as text divergence — realistic, not
    adversarial; real vendors do not agree on markdown conventions for identical content."""
    a = make_envelope("a", text="same text", markdown="# Heading\n\n**same text**")
    b = make_envelope("b", text="same text", markdown="<h1>Heading</h1>\n\n_same text_")
    report = compare([a, b])
    assert _text(report)["matrix"][0][1] == 1.0
    assert not any(f["code"] == "text_divergence" for f in report["findings"])
