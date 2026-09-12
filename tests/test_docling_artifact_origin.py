"""OCR labels survive bounded retrieval and remain part of verified artifact evidence."""

from openreading.artifacts.models import Passage, SearchHit


def test_passages_and_search_hits_carry_measured_text_origin():
    passage = Passage(
        evidence_id="p0001-b0000-s0000",
        page=1,
        block_index=0,
        segment_index=0,
        source_kind="block_text",
        text_origin="ocr",
        text_start=0,
        text_end=4,
        text="word",
    )
    hit = SearchHit(
        evidence_id=passage.evidence_id,
        page=1,
        text_origin=passage.text_origin,
        matched_terms=["word"],
        excerpt_start=0,
        excerpt_end=4,
        excerpt="word",
    )
    assert passage.wire()["text_origin"] == hit.wire()["text_origin"] == "ocr"
