"""M2b — structural dimension findings (DESIGN §4D) through compare(): block unique/missed, type
and position conflicts, table shape, page-count mismatch, and the honesty of `blocks_unavailable`
for a subject that produced no blocks."""

from __future__ import annotations

from openreading.comparison import compare
from tests.fakes import make_envelope


def blk(
    text: str, btype: str = "text", bbox: tuple | None = None, conf: float | None = None
) -> dict:
    b: dict = {"type": btype, "text": text}
    if bbox is not None:
        b["bbox"] = {"x": bbox[0], "y": bbox[1], "w": bbox[2], "h": bbox[3], "page": 1}
    if conf is not None:
        b["confidence"] = conf
    return b


def _codes(report: dict) -> set[str]:
    return {f["code"] for f in report["findings"]}


def _find(report: dict, code: str) -> list[dict]:
    return [f for f in report["findings"] if f["code"] == code]


def test_alignment_object_present() -> None:
    a = make_envelope("a", pages=[[blk("hello world")]])
    b = make_envelope("b", pages=[[blk("hello world")]])
    al = compare([a, b])["alignment"]
    assert al["method"] == "text_first/v1"
    assert al["tau_text"] == 0.6 and al["iou_min"] == 0.3 and al["merge_lookahead"] == 4
    assert al["unaligned_ratio"] == 0.0  # identical → everything aligned


def test_block_unique_two_subjects() -> None:
    a = make_envelope("a", pages=[[blk("common line"), blk("only in a")]])
    b = make_envelope("b", pages=[[blk("common line")]])
    report = compare([a, b])
    uniq = _find(report, "block_unique")
    assert any(f["subjects"] == ["a"] for f in uniq)
    assert "block_missed" not in _codes(report)  # 2 subjects → unique, never missed


def test_block_missed_majority_of_three() -> None:
    a = make_envelope("a", pages=[[blk("shared"), blk("extra one")]])
    b = make_envelope("b", pages=[[blk("shared"), blk("extra one")]])
    c = make_envelope("c", pages=[[blk("shared")]])
    report = compare([a, b, c])
    missed = _find(report, "block_missed")
    assert any(f["subjects"] == ["c"] for f in missed)


def test_block_missed_two_of_four_not_majority() -> None:
    """BL-71: a block found by exactly 2 of 4 jointly block-capable subjects is neither a
    strict majority (2 > 4/2 is False) nor a singleton (len(present) == 2) — the gap between
    `block_unique` and the old majority-gated `block_missed` that let it vanish silently."""
    a = make_envelope("a", pages=[[blk("gap block")]])
    b = make_envelope("b", pages=[[blk("gap block")]])
    c = make_envelope("c", pages=[[blk("c's own block")]])
    d = make_envelope("d", pages=[[blk("d's own block")]])
    report = compare([a, b, c, d])
    missed = _find(report, "block_missed")
    assert {f["subjects"][0] for f in missed} == {"c", "d"}
    assert all(f["severity"] == "warn" for f in missed)


def test_block_missed_clean_three_of_six_tie() -> None:
    """BL-71: a clean 3-of-6 tie (exactly half of six jointly block-capable subjects) is
    neither a strict majority (3 > 6/2 is False) nor a singleton — same gap as the 2-of-4 case,
    reproduced at the tie boundary instead of a plain minority."""
    subj_a = make_envelope("a", pages=[[blk("tie block")]])
    subj_b = make_envelope("b", pages=[[blk("tie block")]])
    subj_c = make_envelope("c", pages=[[blk("tie block")]])
    subj_d = make_envelope("d", pages=[[blk("d's own block")]])
    subj_e = make_envelope("e", pages=[[blk("e's own block")]])
    subj_f = make_envelope("f", pages=[[blk("f's own block")]])
    report = compare([subj_a, subj_b, subj_c, subj_d, subj_e, subj_f])
    missed = _find(report, "block_missed")
    assert {f["subjects"][0] for f in missed} == {"d", "e", "f"}
    assert all(f["severity"] == "warn" for f in missed)


def test_packaging_difference_becomes_structure_finding_not_block_missed() -> None:
    """Tranche 2 (§9, comparison-report v0.2): a block another subject 'missed' whose text IS
    present in that subject's own text is a packaging/granularity difference, not content loss — it
    surfaces as the informational `structure` code, NOT a content-missing block_missed. Here c
    carries the words in its flat text channel but not as an alignable block (the '0'/'2021'
    bank-statement case)."""
    a = make_envelope("a", pages=[[blk("shared"), blk("special marker phrase")]])
    b = make_envelope("b", pages=[[blk("shared"), blk("special marker phrase")]])
    c = make_envelope("c", text="shared special marker phrase", pages=[[blk("shared")]])
    report = compare([a, b, c])
    structure = _find(report, "structure")
    assert structure, "expected a structure finding for c's packaging difference"
    assert all(f["subjects"] == ["c"] and f["severity"] == "info" for f in structure)
    assert not _find(report, "block_missed"), "packaging difference must NOT be a block_missed"


def test_block_missed_stays_warn_when_content_genuinely_absent() -> None:
    """Guard against over-demotion: a block whose text is nowhere in the subject's output is a real
    miss and stays warn (still surfaces the real per-backend misses)."""
    a = make_envelope("a", pages=[[blk("shared"), blk("unique paragraph only a and b have")]])
    b = make_envelope("b", pages=[[blk("shared"), blk("unique paragraph only a and b have")]])
    c = make_envelope("c", pages=[[blk("shared")]])
    missed = _find(compare([a, b, c]), "block_missed")
    assert any(f["subjects"] == ["c"] and f["severity"] == "warn" for f in missed), (
        "a genuinely-absent block must remain a warn block_missed"
    )


def test_type_conflict() -> None:
    a = make_envelope("a", pages=[[blk("Report Title", btype="title")]])
    b = make_envelope("b", pages=[[blk("Report Title", btype="text")]])
    assert "type_conflict" in _codes(compare([a, b]))


def test_position_conflict_when_iou_low() -> None:
    a = make_envelope("a", pages=[[blk("same text here", bbox=(0.0, 0.0, 0.3, 0.1))]])
    b = make_envelope("b", pages=[[blk("same text here", bbox=(0.6, 0.6, 0.3, 0.1))]])
    assert "position_conflict" in _codes(compare([a, b]))


def test_no_position_conflict_when_iou_high() -> None:
    a = make_envelope("a", pages=[[blk("same text here", bbox=(0.0, 0.0, 0.30, 0.10))]])
    b = make_envelope("b", pages=[[blk("same text here", bbox=(0.01, 0.0, 0.30, 0.10))]])
    assert "position_conflict" not in _codes(compare([a, b]))


def test_confidence_gap() -> None:
    a = make_envelope("a", pages=[[blk("line", conf=0.95)]])
    b = make_envelope("b", pages=[[blk("line", conf=0.40)]])
    assert "confidence_gap" in _codes(compare([a, b]))


def test_no_confidence_no_gap() -> None:
    # honesty (L6): a deterministic parser without confidence is never dinged `confidence_gap`
    a = make_envelope("pymupdf", pages=[[blk("line")]])  # no confidence on the block
    b = make_envelope("tesseract", pages=[[blk("line", conf=0.40)]])
    assert "confidence_gap" not in _codes(compare([a, b]))


def test_page_count_mismatch() -> None:
    a = make_envelope("a", pages=[[blk("p one")], [blk("p two")]])
    b = make_envelope("b", pages=[[blk("p one")]])
    assert "page_count_mismatch" in _codes(compare([a, b]))


def test_markdown_only_subject_not_dinged_block_missed() -> None:
    # honesty (L6): a markdown-only backend is not_capable for blocks, never `block_missed`
    a = make_envelope("a", pages=[[blk("a real block")]])
    b = make_envelope("b", markdown="# only markdown, no blocks")
    report = compare([a, b])
    assert any(
        w["code"] == "blocks_unavailable" and w["subject"] == "b" for w in report["warnings"]
    )
    assert not any("b" in f["subjects"] for f in _find(report, "block_missed"))


def test_findings_carry_readable_snippet() -> None:
    # the report is self-explanatory: a block finding includes the block's text (+ confidence)
    a = make_envelope("pymupdf", pages=[[blk("Account Holder:")]])
    b = make_envelope("tesseract", pages=[[blk("; Account Hotder-", conf=0.39)]])
    report = compare([a, b])
    uniq = _find(report, "block_unique")
    snippets = {f["subjects"][0]: f.get("snippet") for f in uniq}
    assert snippets["pymupdf"] == "Account Holder:"
    assert snippets["tesseract"] == "; Account Hotder-"
    # tesseract's garbled block carries its low confidence in the detail
    tess = next(f for f in uniq if f["subjects"] == ["tesseract"])
    assert "0.39" in tess["detail"]


def test_conflict_finding_carries_snippet() -> None:
    a = make_envelope("a", pages=[[blk("Total Balance", "title", bbox=(0.0, 0.0, 0.3, 0.1))]])
    b = make_envelope("b", pages=[[blk("Total Balance", "text", bbox=(0.6, 0.6, 0.3, 0.1))]])
    report = compare([a, b])
    for code in ("type_conflict", "position_conflict"):
        f = next(x for x in report["findings"] if x["code"] == code)
        assert f["snippet"] == "Total Balance"


def test_table_shape_mismatch() -> None:
    def table(rows: list[list[str]]) -> dict:
        return {"type": "table", "text": "grid", "table": {"rows": rows}}

    a = make_envelope("a", pages=[[table([["1", "2"], ["3", "4"]])]])
    b = make_envelope("b", pages=[[table([["1", "2"], ["3", "4"], ["5", "6"]])]])
    assert "table_shape_mismatch" in _codes(compare([a, b]))
