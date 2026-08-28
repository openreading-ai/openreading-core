"""Compare-side hazards (DESIGN §9): each named hazard must NOT produce a false content finding.
These are the compare-side responsibilities the contract can't fix — page-less subjects, aggregator
engine variance, non-determinism."""

from __future__ import annotations

from openreading.comparison import compare
from tests.fakes import make_envelope


def _blk(text: str, btype: str = "text") -> dict:
    return {"type": btype, "text": text}


def test_no_page_boundaries_subject_is_excluded_not_falsely_missed():
    # open-ocr-style: a subject with only document.text (no page boundaries / no blocks) must be
    # excluded from the structural delta with a blocks_unavailable warning — never flagged as
    # missing every block (a false content finding).
    flat = make_envelope("open-ocr", text="the quick brown fox jumps over the lazy dog")
    paged = make_envelope("pymupdf", pages=[[_blk("the quick brown fox jumps over the lazy dog")]])
    report = compare([flat, paged])
    assert "block_missed" not in {f["code"] for f in report["findings"]}
    assert any(w["code"] == "blocks_unavailable" for w in report["warnings"])


def test_aggregator_engines_keyed_on_id_plus_version():
    # ~100 engines behind one backend id: colliding subjects are keyed on id+version so the engines
    # stay distinguishable rather than collapsing into one label.
    a = make_envelope("open-ocr", text="alpha beta gamma delta")
    b = make_envelope("open-ocr", text="alpha beta gamma delta")
    b["backend"]["version"] = "easyocr"
    labels = [s["label"] for s in compare([a, b])["subjects"]]
    assert len(set(labels)) == 2 and any("easyocr" in label for label in labels)


def test_three_subjects_same_id_and_version_get_distinct_labels():
    # BL-119: the second occurrence disambiguates via backend.version ("id (ver)"); a THIRD subject
    # sharing the identical (id, version) pair as the second must not recompute that same label —
    # it must fall back to the #N counter form instead of silently colliding.
    subs = [make_envelope("anthropic-claude", text=f"run {i}") for i in range(3)]
    for s in subs:
        s["backend"]["version"] = "claude-3-5-sonnet-20241022"
    labels = [s["label"] for s in compare(subs)["subjects"]]
    assert len(labels) == len(set(labels)) == 3


def test_four_subjects_same_id_and_version_get_distinct_labels():
    # Same hazard, one subject further: every #N-fallback label issued must itself stay distinct.
    subs = [make_envelope("open-ocr", text=f"run {i}") for i in range(4)]
    for s in subs:
        s["backend"]["version"] = "easyocr"
    labels = [s["label"] for s in compare(subs)["subjects"]]
    assert len(labels) == len(set(labels)) == 4
