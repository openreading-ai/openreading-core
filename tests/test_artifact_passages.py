"""Exact source spans survive segmentation, missing geometry, and physical page gaps."""

import pytest

from openreading.artifacts.passages import iter_passages
from openreading.types.response import NormalizedResponse


def response(pages):
    return NormalizedResponse.model_validate(
        {
            "status": {"state": "succeeded"},
            "backend": {"id": "pymupdf", "type": "oss_library"},
            "document": {"pages": pages, "page_count": 13},
        }
    )


def test_exact_unicode_segments_and_physical_pages():
    original = "notice 😀 Straße " * 150
    result = list(
        iter_passages(
            response(
                [
                    {"page_number": 1, "text": ""},
                    {"page_number": 13, "blocks": [{"type": "text", "text": original}]},
                ]
            )
        )
    )
    assert len(result) > 1
    assert "".join(item.text for item in result) == original
    for index, item in enumerate(result):
        assert item.text == original[item.text_start : item.text_end]
        assert len(item.text) <= 1024
        assert item.page == 13
        assert item.evidence_id == f"p0013-b0000-s{index:04d}"
        assert item.bbox is None


def test_reading_order_and_fallback_are_deterministic():
    pages = [
        {
            "page_number": 3,
            "text": "fallback",
            "blocks": [
                {"type": "text", "text": "second", "reading_order": 9},
                {"type": "text", "text": "first", "reading_order": 0},
            ],
        },
        {"page_number": 4, "text": "page only"},
    ]
    result = list(iter_passages(response(pages)))
    assert [p.text for p in result] == ["first", "second", "page only"]
    assert result[-1].source_kind == "page_text"
    assert [p.evidence_id for p in result] == [
        "p0003-b0000-s0000",
        "p0003-b0001-s0000",
        "p0004-b0000-s0000",
    ]


def test_textless_origin_produces_no_passage():
    assert list(iter_passages(response([{"page_number": 1, "text": " \n"}]), {"1": "none"})) == []


@pytest.mark.parametrize(
    "page",
    [
        {"page_number": 1, "text": "visible"},
        {"page_number": 1, "blocks": [{"type": "text", "text": "visible"}]},
    ],
)
def test_textless_origin_cannot_hide_retained_text(page):
    with pytest.raises(ValueError, match="contradicts retained text"):
        list(iter_passages(response([page]), {"1": "none"}))
