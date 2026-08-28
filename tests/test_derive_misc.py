"""derive.confidence / derive.geometry / derive.pages — the small pure utilities (DESIGN §5)."""

from __future__ import annotations

from openreading.derive import aggregate_confidence, pdf_page_count, utf8_slice


def test_aggregate_confidence_is_min_of_members():
    assert aggregate_confidence([0.9, 0.4, 0.8]) == 0.4  # min, not mean — one bad word floors it
    assert aggregate_confidence([0.95]) == 0.95
    assert aggregate_confidence([]) is None


def test_utf8_slice_uses_byte_offsets_not_codepoints():
    # the google textAnchor bug: API indices are UTF-8 BYTE offsets. "é" is 2 bytes.
    text = "café résumé"
    b = text.encode("utf-8")
    # "café" is bytes 0..5 (c,a,f,é=2 bytes) → 5 bytes
    assert utf8_slice(text, 0, 5) == "café"
    # a naive code-point slice text[0:5] would be "café " (wrong once past the multibyte char)
    assert utf8_slice(text, 6, len(b)) == "résumé"


def test_utf8_slice_handles_split_multibyte_gracefully():
    text = "é"  # 2 bytes: 0xC3 0xA9
    # slicing mid-character must not raise (errors='replace')
    assert isinstance(utf8_slice(text, 0, 1), str)


def test_pdf_page_count_from_bytes():
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    data = doc.tobytes()
    assert pdf_page_count(data) == 2
    assert pdf_page_count(b"not a pdf at all") is None
