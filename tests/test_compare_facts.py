"""Dimension A — run facts (DESIGN §4A), `comparison/facts.py`'s own public surface. Zero direct
test coverage of this module existed at any granularity before this file (BL-136)."""

from __future__ import annotations

from openreading.comparison import Subject
from openreading.comparison.facts import subject_facts
from tests.fakes import make_envelope


def test_chars_matches_canonical_text_regardless_of_markdown_verbosity() -> None:
    """BL-136: `chars` must track the one canonical-text derivation (text-first, DESIGN §4C), not
    a second, hand-rolled markdown-first copy. Two subjects with byte-identical `document.text`
    but differently verbose `document.markdown` (a realistic vendor-to-vendor styling difference,
    not adversarial) must report the same `chars`, equal to `len(text)` — the same combination
    BL-127's own test pins for `canonical_text`, applied here to `facts.py`'s public surface."""
    a = Subject(
        label="a",
        response=make_envelope("a", text="same text", markdown="# Heading\n\n**same text**"),
    )
    b = Subject(
        label="b",
        response=make_envelope(
            "b", text="same text", markdown="<h1>Heading</h1>\n\n_same text_, with far more markup"
        ),
    )
    facts_a = subject_facts(a)
    facts_b = subject_facts(b)
    assert facts_a["chars"] == facts_b["chars"] == len("same text")
