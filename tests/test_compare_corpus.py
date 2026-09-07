"""H5 heterogeneous acceptance corpus + H4 property suite. The live corpus is two genuinely
different real backends (pymupdf × tesseract) on the sample PDF — asserted with structural
invariants, not a byte-golden, because OCR output varies by tesseract version. The property suite
holds for every report: schema-validity, byte-determinism, reflexivity."""

from __future__ import annotations

import json

import pytest

from openreading.comparison import compare
from openreading.schemas import validate_comparison_report
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.testing.tesseract_probe import tesseract_ocr_works
from tests.fakes import make_envelope


def _blk(text: str, btype: str = "text", conf: float | None = None) -> dict:
    b: dict = {"type": btype, "text": text}
    if conf is not None:
        b["confidence"] = conf
    return b


def assert_report(report: dict) -> dict:
    """The shared property gate: every report any test produces is schema-valid."""
    validate_comparison_report(report)
    return report


# --- H5: live heterogeneous corpus ------------------------------------------------------


@pytest.mark.skipif(not tesseract_ocr_works(), reason="tesseract binary not installed or broken")
def test_live_pymupdf_vs_tesseract_structural_invariants() -> None:
    from openreading import api

    pdf = build_sample_pdf()
    a = api.run(pdf, backend="pymupdf", mime_type="application/pdf")
    b = api.run(pdf, backend="tesseract", mime_type="application/pdf")
    report = assert_report(compare([a, b]))

    assert report["mode"] == "pairwise"
    facts = {s["label"]: s["facts"] for s in report["subjects"]}
    assert facts["pymupdf"]["blocks"] > 0 and facts["tesseract"]["blocks"] > 0
    assert 0.0 < report["text"]["matrix"][0][1] <= 1.0
    assert 0.0 <= report["alignment"]["unaligned_ratio"] <= 1.0
    codes = {f["code"] for f in report["findings"]}
    # two backends that segment a page differently must surface at least one structural conflict
    assert codes & {"type_conflict", "position_conflict", "block_unique", "table_shape_mismatch"}


# --- H4: property suite -----------------------------------------------------------------


def _rich_pair() -> tuple[dict, dict]:
    a = make_envelope(
        "a",
        markdown="# Report\nbody one two three",
        fields={"Total": "$100.00", "Date": "2024-01-02"},
        pages=[[_blk("Report", "title", conf=0.95), _blk("body one two three", conf=0.9)]],
    )
    b = make_envelope(
        "b",
        markdown="# Report\nbody one two three four",
        fields={"Total": "100 USD"},  # equivalent value, missing Date
        pages=[[_blk("Report", "text", conf=0.5), _blk("body one two three four", conf=0.8)]],
    )
    return a, b


def test_rich_report_is_schema_valid_and_exercises_all_dimensions() -> None:
    report = assert_report(compare(list(_rich_pair())))
    codes = {f["code"] for f in report["findings"]}
    assert "field_missed" in codes  # b missed Date
    assert "type_conflict" in codes  # title vs text
    assert report["fields"]["rows"]  # field dimension present
    assert report["text"]["matrix"]  # text dimension present
    assert report["blocks"]["pages"]  # block dimension present


def test_report_is_byte_deterministic() -> None:
    a, b = _rich_pair()
    r1 = json.dumps(compare([a, b]), sort_keys=True)
    r2 = json.dumps(compare([a, b]), sort_keys=True)
    assert r1 == r2


def test_reflexivity_on_rich_envelope() -> None:
    a, _ = _rich_pair()
    report = assert_report(compare([a, a]))
    assert all(row["verdict"] == "agree" for row in report["fields"]["rows"])
    assert not any(f["severity"] in ("major", "warn") for f in report["findings"])
    assert any(w["code"] == "subjects_identical" for w in report["warnings"])


def test_nway_facts_and_fields_order_invariant() -> None:
    # facts/fields/text are order-invariant for all N (blocks are anchor-based for N>2, D-v4-14)
    a = make_envelope("a", fields={"Total": "$5"}, text="one two three")
    b = make_envelope("b", fields={"Total": "$9"}, text="one two four")
    c = make_envelope("c", fields={"Total": "$5"}, text="one two three")
    v1 = {r["key"]: r["verdict"] for r in compare([a, b, c])["fields"]["rows"]}
    v2 = {r["key"]: r["verdict"] for r in compare([c, b, a])["fields"]["rows"]}
    assert v1 == v2


def test_every_pair_shape_validates() -> None:
    # a sweep of edge shapes — each must produce a schema-valid report
    cases = [
        (make_envelope("a", text=""), make_envelope("b", text="")),  # both empty
        (
            make_envelope("a", markdown="# x"),
            make_envelope("b", pages=[[_blk("x")]]),
        ),  # md vs blocks
        (make_envelope("a", fields={"K": "v"}), make_envelope("b", text="v")),  # fields vs text
    ]
    for a, b in cases:
        assert_report(compare([a, b]))
