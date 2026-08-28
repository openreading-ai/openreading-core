"""characterize.py — the deterministic line-kind classifier behind `compare --format diffs`'
"the story:" block and per-side header annotations. Four kinds, no LLM: readable text ·
numbers & codes · filler · garbled fragments."""

from __future__ import annotations

from collections import Counter

from openreading.comparison.characterize import (
    KINDS,
    classify_line,
    corpus_story,
    phrase,
)
from openreading.comparison.corpus import corpus_pairs, render_corpus_diffs
from tests.fakes import make_envelope

# ---- classify_line: one pinned example per kind ------------------------------------------------


def test_classify_readable_text():
    assert (
        classify_line("Introducing Business First, a site focused on business.") == "readable text"
    )
    assert classify_line("7815 Woodmont Avenue, Bethesda, MD 20814") == "readable text"


def test_classify_numbers_and_codes():
    assert classify_line("0310005152") == "numbers & codes"
    assert (
        classify_line("PSKEY01|DDA0|01|FTTF|20140531|8|0|S|1|2021|2021|7421664389|")
        == "numbers & codes"
    )
    assert classify_line("03 10005 160") == "numbers & codes"


def test_classify_filler():
    assert classify_line("____________________________________") == "filler"
    assert classify_line("$ _______________________") == "filler"
    assert classify_line("- - - - - - - -") == "filler"
    assert classify_line("·") == "filler"


def test_classify_garbled():
    # vowel-less / implausible alpha tokens — the same character the engine's garble signal keys on
    assert classify_line("HEM XRTQW TLZK PFFT GRXNQ") == "garbled fragments"


def test_every_kind_is_in_the_closed_vocabulary():
    for line in ("hello world today", "12345", "___", "XRTQW PFFT ZZKKJ"):
        assert classify_line(line) in KINDS


# ---- phrase ------------------------------------------------------------------------------------


def test_phrase_mostly_one():
    assert phrase(Counter({"readable text": 9, "filler": 1})) == "mostly readable text"


def test_phrase_mostly_two():
    c = Counter({"numbers & codes": 5, "filler": 4, "readable text": 1})
    assert phrase(c) == "mostly numbers & codes and filler"


def test_phrase_mix_and_empty():
    c = Counter({"readable text": 2, "numbers & codes": 2, "filler": 2, "garbled fragments": 2})
    assert phrase(c).startswith("a mix of")
    assert phrase(Counter()) == "nothing"


# ---- corpus_story ------------------------------------------------------------------------------


def test_corpus_story_orders_by_readable_then_size():
    story = corpus_story(
        {
            "main": Counter(
                {"readable text": 652, "numbers & codes": 248, "garbled fragments": 26}
            ),
            "main_copy": Counter({"numbers & codes": 28, "filler": 14, "readable text": 2}),
        }
    )
    # the side with more readable text leads; both sentences are factual counts + phrases
    assert story.index("main captured 926") < story.index("main_copy")
    assert "mostly readable text" in story
    assert "44" in story and ("numbers & codes" in story and "filler" in story)


def test_corpus_story_zero_side():
    story = corpus_story({"a": Counter({"readable text": 5}), "b": Counter()})
    assert "a captured 5" in story
    assert "b captured nothing a missed" in story


def test_corpus_story_empty_returns_empty():
    assert corpus_story({"a": Counter(), "b": Counter()}) == ""


def test_deterministic():
    c = {"x": Counter({"readable text": 3}), "y": Counter({"filler": 2})}
    assert corpus_story(c) == corpus_story(c)


# ---- integration: the corpus diffs view carries the story + annotated headers ------------------


def _batch(backend: str, items: list[tuple[str, dict]]) -> dict:
    return {
        "schema_version": "0.1",
        "status": {"state": "succeeded"},
        "items": [
            {
                "source": {"relpath": rp, "filename": rp, "sha256": rp + "-hash"},
                "state": "succeeded",
                "response": resp,
            }
            for rp, resp in items
        ],
        "summary": {
            "total": len(items),
            "succeeded": len(items),
            "failed": 0,
            "skipped": 0,
            "backends": {backend: len(items)},
        },
    }


def test_render_corpus_diffs_carries_story_and_header_kinds():
    a = _batch(
        "pulse",
        [
            (
                "d.pdf",
                make_envelope(
                    "pulse",
                    text="The quarterly revenue grew nicely\nOffice furniture was liquidated today\nExtra prose only pulse captured here",
                ),
            )
        ],
    )
    b = _batch(
        "pymupdf",
        [
            (
                "d.pdf",
                make_envelope(
                    "pymupdf", text="____________________\n0310005152\n99887 12345 55443"
                ),
            )
        ],
    )
    out = render_corpus_diffs(corpus_pairs([a, b], ["runA", "runB"]), ["runA", "runB"])
    assert "the story:" in out
    assert "readable text" in out  # runA's character
    assert "numbers & codes" in out or "filler" in out  # runB's character
    assert "— mostly" in out  # annotated per-side header


def test_render_corpus_diffs_summary_table_before_details():
    a = _batch(
        "pulse",
        [
            (
                "d.pdf",
                make_envelope("pulse", text="Real prose sentence here\nAnother line of words"),
            ),
            ("same.pdf", make_envelope("pulse", text="identical on both sides")),
        ],
    )
    b = _batch(
        "pymupdf",
        [
            ("d.pdf", make_envelope("pymupdf", text="____________\n0310005152")),
            ("same.pdf", make_envelope("pymupdf", text="identical on both sides")),
        ],
    )
    out = render_corpus_diffs(corpus_pairs([a, b], ["runA", "runB"]), ["runA", "runB"])
    # a summary table with one row per document, columns per side, before the details
    assert "only runA" in out and "only runB" in out and "shared" in out
    assert out.index("only runA") < out.index("details (the lines behind the counts):")
    # the equivalent doc appears in the table with its verdict, not side cells
    table_part = out[: out.index("details (")]
    assert "same.pdf" in table_part and "equivalent" in table_part


def test_render_corpus_diffs_no_story_when_all_equivalent():
    same = "identical text on both sides here"
    a = _batch("pulse", [("d.pdf", make_envelope("pulse", text=same))])
    b = _batch("pymupdf", [("d.pdf", make_envelope("pymupdf", text=same))])
    out = render_corpus_diffs(corpus_pairs([a, b], ["runA", "runB"]), ["runA", "runB"])
    assert "the story:" not in out
