"""Compare tranche 2 (DESIGN §9, comparison-report v0.2): content-first headline + the
non-determinism rule (generative subjects compared by similarity; their content findings cap at
informational)."""

from __future__ import annotations

from openreading.comparison import compare
from tests.fakes import make_envelope


def _blk(text: str, btype: str = "text") -> dict:
    return {"type": btype, "text": text}


def _tbl(rows: list[list[str]], text: str = "grid") -> dict:
    return {"type": "table", "text": text, "table": {"rows": rows}}


def _find(report: dict, code: str) -> list[dict]:
    return [f for f in report["findings"] if f["code"] == code]


def _codes(report: dict) -> set[str]:
    return {f["code"] for f in report["findings"]}


def test_report_carries_content_first_headline():
    a = make_envelope("a", text="hello world", pages=[[_blk("hello world")]])
    b = make_envelope("b", text="hello world", pages=[[_blk("hello world")]])
    hl = compare([a, b])["headline"]
    assert hl["verdict"] == "equivalent"
    assert hl["channels"]["text"]["agreement"] == "agree"
    assert hl["nondeterministic_subjects"] == []


def test_headline_diverges_on_text_mismatch():
    a = make_envelope(
        "a", text="the quick brown fox jumps over", pages=[[_blk("the quick brown fox jumps over")]]
    )
    b = make_envelope(
        "b",
        text="wholly unrelated content appears instead",
        pages=[[_blk("wholly unrelated content appears instead")]],
    )
    hl = compare([a, b])["headline"]
    assert hl["channels"]["text"]["agreement"] == "diverge"
    assert hl["verdict"] in ("divergent", "mixed")


def test_nondeterministic_subject_caps_content_findings_to_info():
    # anthropic-claude is in the documented generative set; its text_divergence caps at info.
    a = make_envelope(
        "anthropic-claude", text="the quick brown fox", pages=[[_blk("the quick brown fox")]]
    )
    b = make_envelope(
        "pymupdf",
        text="entirely unrelated words here now",
        pages=[[_blk("entirely unrelated words here now")]],
    )
    report = compare([a, b])
    tdiv = _find(report, "text_divergence")
    assert tdiv, "expected a text_divergence"
    assert all(f["severity"] == "info" for f in tdiv)  # capped by the non-determinism rule
    assert "anthropic-claude" in report["headline"]["nondeterministic_subjects"]


def test_deterministic_subjects_keep_warn_text_divergence():
    a = make_envelope("pymupdf", text="the quick brown fox", pages=[[_blk("the quick brown fox")]])
    b = make_envelope(
        "tesseract",
        text="entirely unrelated words here now",
        pages=[[_blk("entirely unrelated words here now")]],
    )
    tdiv = _find(compare([a, b]), "text_divergence")
    assert tdiv and any(f["severity"] == "warn" for f in tdiv)  # deterministic parsers stay warn


# --- BL-58: table_cells must not read "agree" when only one subject is block-capable -----------


def test_table_cells_is_partial_not_agree_when_only_one_subject_is_block_capable():
    # The exact repro: a real 2x2 table from a block-producing backend (aws-textract) compared
    # against a block-less, token-stream backend (open-ocr). blocks_section never runs its table
    # comparison (needs >=2 jointly capable subjects), so `table_shape_mismatch` was structurally
    # incapable of firing — "agree" would be unverified, not genuine agreement.
    a = make_envelope("aws-textract", pages=[[_tbl([["1", "2"], ["3", "4"]])]])
    b = make_envelope("open-ocr", text="a completely different, block-less transcription")
    report = compare([a, b])
    assert report["alignment"]["capable_subjects"] == 1
    assert "table_shape_mismatch" not in _codes(report)
    channel = report["headline"]["channels"]["table_cells"]
    assert channel["agreement"] == "partial"
    assert channel["agreement"] != "agree"
    assert report["headline"]["verdict"] != "equivalent"


def test_table_cells_still_diverges_when_both_subjects_are_block_capable():
    # Sibling case: both sides ARE jointly block-capable, so a real shape mismatch still diverges
    # — the BL-58 fix must not blunt the channel when the comparison genuinely ran.
    a = make_envelope("a", pages=[[_tbl([["1", "2"], ["3", "4"]])]])
    b = make_envelope("b", pages=[[_tbl([["1", "2"], ["3", "4"], ["5", "6"]])]])
    report = compare([a, b])
    assert report["alignment"]["capable_subjects"] == 2
    assert "table_shape_mismatch" in _codes(report)
    assert report["headline"]["channels"]["table_cells"]["agreement"] == "diverge"
