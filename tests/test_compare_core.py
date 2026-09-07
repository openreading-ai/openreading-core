"""M1 core — facts scoreboard + field delta, plus the invariants that must hold for every report
(schema-validity, reflexivity, order-invariance, capability honesty) and the leaf-isolation guard
(H4/H6). Compare is pure over envelopes, so every case is a hand-built envelope — no I/O."""

from __future__ import annotations

import json
import pathlib

import pytest

import openreading
from openreading.comparison import CompareInputError, compare
from openreading.schemas import validate_comparison_report
from tests.fakes import make_envelope


def _subj(report: dict, label: str) -> dict:
    return next(s for s in report["subjects"] if s["label"] == label)


def _row(report: dict, key: str) -> dict:
    return next(r for r in report["fields"]["rows"] if r["key"] == key)


def _codes(report: dict) -> list[str]:
    return sorted(f["code"] for f in report["findings"])


# --- shape / schema ---------------------------------------------------------------------


def test_compare_is_exported() -> None:
    assert openreading.compare is compare
    assert "compare" in openreading.__all__


def test_report_is_schema_valid_and_pairwise() -> None:
    a = make_envelope("a", fields={"Total": "$5.00"})
    b = make_envelope("b", fields={"Total": "$5.00"})
    report = compare([a, b])
    validate_comparison_report(report)  # redundant with build_report, but asserts the contract
    assert report["schema_version"] == "0.2"
    assert report["mode"] == "pairwise"
    assert {s["label"] for s in report["subjects"]} == {"a", "b"}


def test_nway_mode_for_three_subjects() -> None:
    subs = [make_envelope(x, fields={"Total": "$5"}) for x in ("a", "b", "c")]
    report = compare(subs)
    assert report["mode"] == "nway"
    assert len(report["subjects"]) == 3


# --- dimension A: facts scoreboard ------------------------------------------------------


def test_scoreboard_facts() -> None:
    a = make_envelope(
        "pymupdf",
        backend_type="oss_library",
        pages=[[{"type": "title", "text": "Hi"}, {"type": "text", "text": "world"}]],
        duration_ms=12,
    )
    b = make_envelope("tesseract", backend_type="oss_library", text="x")
    facts = _subj(compare([a, b]), "pymupdf")["facts"]
    assert facts == {
        "state": "succeeded",
        "duration_ms": 12,
        "pages": 1,
        "blocks": 2,
        # BL-136: chars is the canonical-text derivation's own block fallback — blocks joined in
        # reading order with "\n" — not a second, hand-rolled sum of block-text lengths.
        "chars": len("Hi\nworld"),
        "fields": 0,
        "warning_codes": [],
    }


def test_warning_codes_collected_and_propagated() -> None:
    a = make_envelope("pymupdf", text="x", warnings=["confidence_unavailable"])
    b = make_envelope("tesseract", text="x")
    report = compare([a, b])
    assert _subj(report, "pymupdf")["facts"]["warning_codes"] == ["confidence_unavailable"]
    assert any(
        w["code"] == "confidence_unavailable" and w["subject"] == "pymupdf"
        for w in report["warnings"]
    )


def test_empty_output_finding() -> None:
    a = make_envelope("a", text="hello")
    b = make_envelope("b")  # no text, blocks, or fields
    report = compare([a, b])
    assert any(f["code"] == "empty_output" and f["subjects"] == ["b"] for f in report["findings"])


# --- dimension B: field delta -----------------------------------------------------------


def test_fields_agree_via_money_equivalence() -> None:
    a = make_envelope("a", fields={"Total": "$4,400.00"})
    b = make_envelope("b", fields={"Total": "4400.00 USD"})
    report = compare([a, b])
    row = _row(report, "Total")
    assert row["verdict"] == "agree"
    assert row["by_subject"]["b"]["equivalence"] == "money"
    assert "field_value_conflict" not in _codes(report)


def test_fields_agree_via_money_equivalence_signal_less_side() -> None:
    # BL-36: one backend preserves the amount as printed ("$4,400.00"), the other normalizes it
    # to a bare numeric string ("4400") with no currency signal at all — the ladder must still
    # bridge them, and the report's own top-level headline verdict must not flip to "mixed".
    a = make_envelope("a", fields={"Total": "$4,400.00"})
    b = make_envelope("b", fields={"Total": "4400"})
    report = compare([a, b])
    row = _row(report, "Total")
    assert row["verdict"] == "agree"
    assert row["by_subject"]["b"]["equivalence"] in ("money", "number")
    assert "field_value_conflict" not in _codes(report)
    assert report["headline"]["verdict"] == "equivalent"


def test_field_null_but_present_classifies_as_missed_not_conflict() -> None:
    # BL-109: an explicit `null` value (a schema-driven extractor's honest "couldn't confidently
    # fill this" — reducto/nuextract both do this for real) is incomparable, not "disagrees" with
    # a real value — equivalence_tier(None, x) is always None by contract ("incomparable"), which
    # every caller here used to silently reinterpret as "these subjects disagree". It must get the
    # same field_missed/warn treatment as an omitted key, never field_value_conflict/major.
    a = make_envelope("a", fields={"Total": "$100"})
    b = make_envelope("b", fields={"Total": None})
    report = compare([a, b])
    row = _row(report, "Total")
    assert row["verdict"] == "unique"
    assert row["by_subject"]["b"]["present"] is False
    assert row["by_subject"]["b"]["equivalence"] is None
    assert "field_value_conflict" not in _codes(report)
    missed = [f for f in report["findings"] if f["code"] == "field_missed"]
    assert missed and missed[0]["subjects"] == ["b"]


def test_field_null_value_does_not_poison_agreeing_subjects_equivalence() -> None:
    # BL-109 amplification: two subjects that agree byte-for-byte must keep their own "exact"
    # equivalence signal even when a third subject's null sits in the same row — before the fix,
    # equivalence_tier(None, x) being always-None poisoned the row's complete-linkage check for
    # EVERY subject, not just the null-valued one, so a and b would both lose "exact" too.
    a = make_envelope("a", fields={"Total": "$100"})
    b = make_envelope("b", fields={"Total": "$100"})
    c = make_envelope("c", fields={"Total": None})
    report = compare([a, b, c])
    row = _row(report, "Total")
    assert row["verdict"] == "partial"
    assert row["by_subject"]["a"]["equivalence"] == "exact"
    assert row["by_subject"]["b"]["equivalence"] == "exact"
    assert row["by_subject"]["c"]["present"] is False
    assert "field_value_conflict" not in _codes(report)
    missed = [f for f in report["findings"] if f["code"] == "field_missed"]
    assert missed and missed[0]["subjects"] == ["c"]


def test_fields_disagree_emits_conflict() -> None:
    a = make_envelope("a", fields={"Total": "$100"})
    b = make_envelope("b", fields={"Total": "$200"})
    report = compare([a, b])
    assert _row(report, "Total")["verdict"] == "disagree"
    conflict = next(f for f in report["findings"] if f["code"] == "field_value_conflict")
    assert conflict["field"] == "Total"
    assert conflict["severity"] == "major"
    assert sorted(conflict["subjects"]) == ["a", "b"]


def test_field_unique_and_missed() -> None:
    a = make_envelope("a", fields={"Total": "$5", "Date": "2024-01-01"})
    b = make_envelope("b", fields={"Total": "$5"})
    report = compare([a, b])
    assert _row(report, "Total")["verdict"] == "agree"
    assert _row(report, "Date")["verdict"] == "unique"
    missed = [f for f in report["findings"] if f["code"] == "field_missed"]
    assert len(missed) == 1
    assert missed[0]["field"] == "Date" and missed[0]["subjects"] == ["b"]


def test_partial_when_capable_subject_absent() -> None:
    # three subjects, two agree on a field the third (a field-capable backend) missed
    a = make_envelope("a", fields={"Total": "$5"})
    b = make_envelope("b", fields={"Total": "$5"})
    c = make_envelope("c", fields={"Other": "z"})  # capable (produced a field), missed Total
    report = compare([a, b, c])
    assert _row(report, "Total")["verdict"] == "partial"


def test_present_absent_present_same_id_version_scores_partial_not_agree() -> None:
    # BL-119: three subjects share the identical (backend.id, backend.version) pair, and the field
    # is present/absent/present in subject order — the 2nd (absent) and 3rd (present) subjects are
    # exactly the pair that pre-fix collided on the same "(ver)"-derived label, so the 3rd's
    # by_subject write silently clobbered the 2nd's "absent" entry. A verdict-only assertion can
    # pass today for the wrong reason on some orderings, so this also pins by_subject's length.
    a = make_envelope("dup", fields={"Total": "$5"})
    b = make_envelope("dup", fields={"Other": "z"})  # capable (produced a field), missed Total
    c = make_envelope("dup", fields={"Total": "$5"})
    for s in (a, b, c):
        s["backend"]["version"] = "v1"
    report = compare([a, b, c])
    assert len({s["label"] for s in report["subjects"]}) == 3  # no two subjects share a label
    row = _row(report, "Total")
    assert len(row["by_subject"]) == 3
    assert row["verdict"] == "partial"
    assert row["verdict"] not in ("agree", "unique", "disagree")
    assert "field_missed" in _codes(report)
    assert report["headline"]["verdict"] == "mixed"
    assert report["headline"]["verdict"] != "equivalent"


def test_normalized_key_grouping() -> None:
    a = make_envelope("a", fields={"Total": "$5"})
    b = make_envelope("b", fields={"total ": "$5"})
    report = compare([a, b])
    rows = report["fields"]["rows"]
    assert len(rows) == 1
    assert rows[0]["key"] == "Total" and rows[0]["verdict"] == "agree"


def test_pymupdf_not_dinged_for_missing_fields() -> None:
    # honesty (L6): a parser that does not do extraction is never faulted for absent typed_fields
    parser = make_envelope("pymupdf", backend_type="oss_library", text="body text")
    extractor = make_envelope("reducto", fields={"Total": "$5"})
    report = compare([parser, extractor])
    assert not any(
        f["code"] == "field_missed" and "pymupdf" in f["subjects"] for f in report["findings"]
    )


# --- invariants -------------------------------------------------------------------------


def test_reflexivity() -> None:
    r = make_envelope(
        "x",
        fields={"Total": "$4,400.00", "Date": "2024-01-02"},
        pages=[[{"type": "text", "text": "hi"}]],
    )
    report = compare([r, r])
    assert all(row["verdict"] == "agree" for row in report["fields"]["rows"])
    assert not any(f["severity"] in ("major", "warn") for f in report["findings"])
    assert any(w["code"] == "subjects_identical" for w in report["warnings"])


def test_order_invariance() -> None:
    a = make_envelope("a", fields={"Total": "$100", "Only": "z"})
    b = make_envelope("b", fields={"Total": "$200"})
    r1, r2 = compare([a, b]), compare([b, a])
    v1 = {row["key"]: row["verdict"] for row in r1["fields"]["rows"]}
    v2 = {row["key"]: row["verdict"] for row in r2["fields"]["rows"]}
    assert v1 == v2
    assert _codes(r1) == _codes(r2)


def test_determinism_byte_identical() -> None:
    a = make_envelope("a", fields={"Total": "$100"})
    b = make_envelope("b", fields={"Total": "$200"})
    assert json.dumps(compare([a, b]), sort_keys=True) == json.dumps(
        compare([a, b]), sort_keys=True
    )


# --- ingestion / signature --------------------------------------------------------------


def test_compare_accepts_paths(tmp_path: pathlib.Path) -> None:
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(make_envelope("a", fields={"Total": "$5"})))
    pb.write_text(json.dumps(make_envelope("b", fields={"Total": "$5"})))
    report = compare([str(pa), str(pb)])
    assert report["mode"] == "pairwise"


def test_requires_two_subjects() -> None:
    with pytest.raises(CompareInputError):
        compare([make_envelope("a")])


def test_rejects_non_response_input() -> None:
    with pytest.raises(CompareInputError):
        compare([{"not": "a response"}, make_envelope("b")])


def test_invalid_input_message_is_one_line_without_the_schema() -> None:
    # A jsonschema ValidationError stringifies to the whole response schema, ~995 lines of it.
    # Interpolating the exception turned exit 5 into a terminal-filling dump that reads as a
    # crash, so only the message and the instance path reach the reader.
    with pytest.raises(CompareInputError) as caught:
        compare([{"hello": 1}, make_envelope("b")])
    message = str(caught.value)
    assert message == (
        "input #1 is not a schema-valid response: 'schema_version' is a required property (at $)"
    )


def test_baseline_and_truth_stances_produce_sections() -> None:
    a = make_envelope("a", fields={"Total": "$5"}, text="hi")
    b = make_envelope("b", fields={"Total": "$5"}, text="hi")
    assert compare([a, b], baseline="a")["baseline"]["baseline"] == "a"
    assert "truth" in compare([a, b], truth={"text": "hi"})


# --- H6a: leaf isolation ----------------------------------------------------------------


def test_comparison_not_imported_by_router_or_strategies() -> None:
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "openreading"
    offenders = []
    for pkg in ("router", "strategies"):
        for f in (root / pkg).rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            if "openreading.comparison" in text or "import comparison" in text:
                offenders.append(str(f))
    assert not offenders, f"comparison must stay a leaf; imported by: {offenders}"
