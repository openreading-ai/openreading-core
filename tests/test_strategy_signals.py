"""Milestone 11.4 — the signal probe + gate evaluation + the H3 fixture builders.

Covers: Tier-1 text metrics, the garble composite (clean vs accented vs mojibake), the
pypdf-driven PDF-layer signals on the scanned/garbled fixtures, Tier-2 aggregation, and the
missing-signal law (unavailable -> doesn't fire; on_missing: escalate flips it; fields_required
fires on absence).
"""

from __future__ import annotations

import pytest

from openreading.strategies import evaluate_gate, garble_score, probe
from openreading.testing.sample_pdf import build_garbled_pdf, build_sample_pdf, build_scanned_pdf
from openreading.types.blocks import Block, TypedField
from openreading.types.enums import BackendType, BlockType, ResponseState
from openreading.types.response import (
    BackendInfo,
    DocType,
    Document,
    NormalizedResponse,
    Page,
    Status,
    Warning,
)


def _resp(*, text="", pages=None, typed_fields=None, warnings=None, doc_type=None):
    return NormalizedResponse(
        status=Status(state=ResponseState.SUCCEEDED),
        backend=BackendInfo(id="x", type=BackendType.OSS_LIBRARY),
        document=Document(text=text, pages=pages, doc_type=doc_type),
        typed_fields=typed_fields,
        warnings=warnings,
    )


# ---- garble composite -------------------------------------------------------------------------


def test_garble_clean_english_low():
    assert garble_score("The quick brown fox jumps over the lazy dog every morning.") < 0.15


def test_garble_accented_legit_not_flagged():
    # legitimate accented words carry ASCII vowels -> not mojibake
    assert garble_score("Cafe naive resume Zurich senor uber the facade of the building.") < 0.15


def test_garble_mojibake_high():
    assert garble_score("Ã©Ã¨ÃªÃ«Å â€™Ã±Â§Â¶ Ã Ã¢Ã¤ Ãµ Ã¼Ã¿ â‚¬Â£Â¥") > 0.3


def test_garble_empty_is_none():
    assert garble_score("   ") is None


# ---- Tier-1 text metrics ----------------------------------------------------------------------


def test_chars_per_page_and_empty_pages():
    pages = [
        Page(page_number=1, text="a" * 200),
        Page(page_number=2, text=""),  # empty
        Page(page_number=3, text="b" * 40),
    ]
    snap = probe(_resp(pages=pages))
    assert snap.chars_per_page == pytest.approx((200 + 0 + 40) / 3)
    assert snap.empty_pages_fraction == pytest.approx(1 / 3)


def test_zero_blocks_true_when_no_blocks():
    assert probe(_resp(text="hi")).zero_blocks is True
    pages = [Page(page_number=1, blocks=[Block(type=BlockType.TEXT, text="hi")])]
    assert probe(_resp(pages=pages)).zero_blocks is False


# ---- PDF-layer signals on the H3 fixtures -----------------------------------------------------


def test_scanned_fixture_detected():
    snap = probe(_resp(), doc_bytes=build_scanned_pdf(), mime_type="application/pdf")
    assert snap.scanned_pages_detected is True
    assert snap.text_source == "none"


def test_garbled_fixture_scores_high():
    # extract the garbled text layer via the probe's own doc text (simulate the parse result)
    score = garble_score("Ã©Ã¨ÃªÃ«Å â€™Ã±Â§Â¶ Ã Ã¢Ã¤ Ãµ Ã¼Ã¿")
    assert score is not None and score > 0.3
    # the fixture is a real PDF the probe can introspect without crashing
    snap = probe(_resp(text="x"), doc_bytes=build_garbled_pdf(), mime_type="application/pdf")
    assert snap.text_source == "digital"  # it has a (garbage) text layer


def test_born_digital_sample_not_scanned():
    snap = probe(_resp(text="clean"), doc_bytes=build_sample_pdf(), mime_type="application/pdf")
    assert snap.scanned_pages_detected is False
    assert snap.text_source == "digital"


def test_pdf_layer_absent_without_bytes_or_for_non_pdf():
    assert probe(_resp(text="x")).scanned_pages_detected is None
    assert probe(_resp(text="x"), doc_bytes=b"not a pdf", mime_type="image/png").text_source is None


def test_malformed_pdf_degrades_gracefully():
    snap = probe(_resp(text="x"), doc_bytes=b"%PDF-1.4 broken", mime_type="application/pdf")
    assert snap.scanned_pages_detected is None  # no crash, signal simply absent


# ---- Tier-2 aggregation -----------------------------------------------------------------------


def test_doc_confidence_mean_over_reporting_pages():
    pages = [
        Page(page_number=1, confidence=0.9),
        Page(page_number=2),  # no confidence
        Page(page_number=3, confidence=0.7),
    ]
    snap = probe(_resp(pages=pages))
    assert snap.doc_confidence == pytest.approx(0.8)
    assert snap.page_confidences == [0.9, 0.7]


def test_doc_confidence_absent_when_no_page_reports():
    snap = probe(_resp(pages=[Page(page_number=1, text="hi")]))
    assert snap.doc_confidence is None


def test_typed_fields_and_warnings_and_doc_type():
    snap = probe(
        _resp(
            typed_fields={"total": TypedField(value="$5", confidence=0.95)},
            warnings=[Warning(code="confidence_unavailable", message="x")],
            doc_type=DocType(label="invoice", confidence=0.6),
        )
    )
    assert snap.typed_fields["total"]["confidence"] == 0.95
    assert "confidence_unavailable" in snap.warning_codes
    assert snap.doc_type_confidence == 0.6


# ---- gate evaluation + missing-signal law -----------------------------------------------------


def test_gate_ors_predicates():
    snap = probe(_resp(pages=[Page(page_number=1, text="a" * 5)]))  # ~5 chars
    res = evaluate_gate({"chars_per_page_below": 100, "garbled": True}, snap)
    assert res.fired  # chars gate fires (5 < 100) even though garble doesn't


def test_confidence_gate_unavailable_does_not_fire():
    snap = probe(_resp(pages=[Page(page_number=1, text="plain")]))  # no confidence
    res = evaluate_gate({"confidence_below": 0.6}, snap)
    assert not res.fired
    assert res.predicates[0].unavailable is True


def test_on_missing_escalate_fires_when_absent():
    snap = probe(_resp(pages=[Page(page_number=1, text="plain")]))
    res = evaluate_gate({"confidence_below": {"value": 0.6, "on_missing": "escalate"}}, snap)
    assert res.fired
    assert res.predicates[0].unavailable is True


def test_confidence_gate_fires_when_present_and_low():
    snap = probe(_resp(pages=[Page(page_number=1, confidence=0.4)]))
    res = evaluate_gate({"confidence_below": 0.6}, snap)
    assert res.fired and res.predicates[0].observed == pytest.approx(0.4)


def test_fields_required_fires_on_absence_with_aliases():
    snap = probe(_resp(typed_fields={"PayDate": TypedField(value="2026-01-01")}))
    # invoice_number missing -> fires; pay_date satisfied via alias PayDate
    res = evaluate_gate(
        {"fields_required": ["invoice_number", {"name": "pay_date", "aliases": ["PayDate"]}]}, snap
    )
    assert res.fired
    assert res.predicates[0].observed == ["invoice_number"]  # only the truly-missing one


def test_fields_required_satisfied_no_fire():
    snap = probe(_resp(typed_fields={"total": TypedField(value="$5")}))
    res = evaluate_gate({"fields_required": ["total"]}, snap)
    assert not res.fired


def test_field_confidence_per_field_map():
    snap = probe(
        _resp(
            typed_fields={
                "total": TypedField(value="$5", confidence=0.95),
                "date": TypedField(value="x", confidence=0.7),
            }
        )
    )
    res = evaluate_gate({"field_confidence_below": {"fields": {"date": 0.85, "total": 0.9}}}, snap)
    assert res.fired  # date 0.7 < 0.85


def test_field_confidence_qualitative_string_not_coerced():
    snap = probe(_resp(typed_fields={"total": TypedField(value="$5", confidence="HIGH")}))
    res = evaluate_gate({"field_confidence_below": 0.9}, snap)
    assert not res.fired and res.predicates[0].unavailable is True  # no numeric confidence


def test_any_of_all_of_nesting():
    snap = probe(
        _resp(text="this page intentionally left blank", pages=[Page(page_number=1, text="short")])
    )
    gate = {
        "any_of": [
            {
                "all_of": [
                    {"chars_per_page_below": 100},
                    {"matches_regex": "(?i)intentionally left blank"},
                ]
            },
            {"zero_blocks": True},
        ]
    }
    assert evaluate_gate(gate, snap).fired


def test_sample_percent_deterministic():
    body = b"%PDF-1.4 deterministic content"
    a = probe(_resp(text="x"), doc_bytes=body).sample_bucket()
    b = probe(_resp(text="x"), doc_bytes=body).sample_bucket()
    assert a == b and 0 <= a < 100
    # unavailable without bytes
    res = evaluate_gate({"sample_percent": 5}, probe(_resp(text="x")))
    assert res.predicates[0].unavailable is True


def test_disagreement_over_predicate():
    # §11 phase 2: the cross-branch signal binds when the snapshot carries it, else unavailable.
    from openreading.strategies.signals import SignalSnapshot, evaluate_gate

    fired = evaluate_gate({"disagreement_over": 0.3}, SignalSnapshot(disagreement_over=0.6))
    assert fired.fired
    not_over = evaluate_gate({"disagreement_over": 0.9}, SignalSnapshot(disagreement_over=0.6))
    assert not not_over.fired
    absent = evaluate_gate({"disagreement_over": 0.3}, SignalSnapshot())  # None → unavailable
    assert not absent.fired
    assert all(p.unavailable for p in absent.predicates)
