"""Eval harness — scorers on synthetic inputs + an end-to-end run of the PyMuPDF adapter over the
sample dataset (executed for real). Proves the harness produces comparable numbers for any backend
via the one NormalizedResponse."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openreading.evals import (
    EvalCase,
    field_prf,
    load_dataset,
    run_case,
    run_dataset,
    score,
    table_grid,
    text_similarity,
)
from openreading.evals.dataset import load_case
from openreading.evals.scorers import contains_fraction
from openreading.router import RouterConfig
from tests.fakes import ConfigurableBackend, make_backend

SAMPLE = Path("src/openreading/evals/sample")


# --- scorers ----------------------------------------------------------------------------


def test_text_similarity_normalizes_whitespace_and_case():
    assert text_similarity("Hello  World", "hello world") == pytest.approx(1.0)
    assert text_similarity("", "") == 1.0
    assert text_similarity("totally different", "xxxxx") < 0.4


def test_contains_fraction():
    assert contains_fraction("the quick brown fox", ["quick", "fox"]) == 1.0
    assert contains_fraction("the quick brown fox", ["quick", "zebra"]) == 0.5


def test_field_prf_precision_recall_f1():
    got = {"total": "100", "vendor": "Acme", "extra": "x"}
    expected = {"total": "100", "vendor": "Acme", "date": "2026"}
    s = field_prf(got, expected)
    assert s["correct"] == 2
    assert s["precision"] == pytest.approx(2 / 3)  # 2 of 3 emitted are right
    assert s["recall"] == pytest.approx(2 / 3)  # 2 of 3 expected found
    assert s["f1"] == pytest.approx(2 / 3)


def test_table_grid_exact_and_tolerant():
    exp = [["A", "B"], ["1", "2"]]
    assert table_grid([["A", "B"], ["1", "2"]], exp)["exact"] == 1.0
    partial = table_grid([["A", "B"], ["1", "9"]], exp)
    assert partial["exact"] == 0.0 and partial["cell_accuracy"] == pytest.approx(0.75)
    # tolerant normalizes case/whitespace
    assert table_grid([[" a ", "b"], ["1", "2"]], exp, tolerant=True)["exact"] == 1.0


def test_score_combines_present_dimensions_only():
    resp = {"document": {"text": "Loan Application", "pages": []}}
    s = score(resp, {"text_contains": ["Loan"]})
    assert "text_contains" in s["dimensions"] and "field_prf" not in s["dimensions"]
    assert s["overall"] == pytest.approx(1.0)


def test_score_with_no_recognized_dimension_is_unscored_not_false_perfect():
    # expected={} names none of the five scored dimensions — an ordinary "not labeled yet" shape,
    # indistinguishable from "every measured dimension came back perfect" under the old 1.0
    # default. Garbled, obviously-wrong text must not score a perfect overall (BL-79/Jin).
    resp = {"document": {"text": "totally garbled unreadable output nothing like the source"}}
    s = score(resp, {})
    assert s["overall"] is None
    assert s["dimensions"] == {}


def test_score_with_unrecognized_key_is_also_unscored():
    # a key that isn't one of the five recognized dimensions is exactly as unscored as {} —
    # dims stays empty either way.
    resp = {"document": {"text": "irrelevant"}}
    s = score(resp, {"unrecognized_key": 1})
    assert s["overall"] is None
    assert s["dimensions"] == {}


def test_score_tables_empty_expected_is_satisfied_by_a_response_with_no_tables():
    # tables: [] is the natural way to assert "this document has no tables, don't hallucinate
    # one" (BL-95) — a response that genuinely produced none must score a perfect 1.0, not the
    # `else 0.0` fallback the empty-list loop used to fall through to.
    resp_with_no_tables = {"document": {"text": "no tables in this document", "pages": []}}
    s = score(resp_with_no_tables, {"tables": []})
    assert s["dimensions"]["table_cell_accuracy"] == 1.0


def test_score_tables_empty_expected_is_violated_by_a_hallucinated_table():
    # The one real failure mode "tables: []" asserts against: the response fabricated a table
    # that was never supposed to be there.
    resp_with_a_table = {
        "document": {
            "pages": [
                {
                    "page_number": 1,
                    "blocks": [{"type": "table", "table": {"rows": [["A", "B"], ["1", "2"]]}}],
                }
            ]
        }
    }
    s = score(resp_with_a_table, {"tables": []})
    assert s["dimensions"]["table_cell_accuracy"] == 0.0


# --- dataset + end-to-end run -----------------------------------------------------------


def test_sample_dataset_loads():
    cases = load_dataset(SAMPLE, backend_id="pymupdf")
    assert len(cases) == 1
    c = cases[0]
    assert c.name == "loan_page1"
    assert c.request_body["document"]["mime_type"] == "application/pdf"
    assert "text_contains" in c.expected


# --- path containment + input-form coverage (BL-46) ------------------------------------


def _write_case(tmp_path: Path, subdir: str, spec: dict) -> Path:
    case_dir = tmp_path / subdir
    case_dir.mkdir(parents=True, exist_ok=True)
    case_json = case_dir / "case.json"
    case_json.write_text(json.dumps(spec))
    return case_json


def test_resolve_document_path_rejects_parent_traversal(tmp_path):
    # canary sits two directories above the case dir, referenced by relative ../.. traversal
    canary = tmp_path / "canary.pdf"
    canary.write_bytes(b"canary bytes")
    case_json = _write_case(
        tmp_path,
        "dataset/case_a",
        {"name": "traversal", "input": {"path": "../../canary.pdf"}, "expected": {}},
    )
    with pytest.raises(ValueError, match="escapes case_dir"):
        load_case(case_json, backend_id="pymupdf")


def test_resolve_document_path_rejects_absolute_path(tmp_path):
    # no ../ traversal syntax at all — Path.__truediv__ discards case_dir for an absolute rhs
    canary = tmp_path / "canary.pdf"
    canary.write_bytes(b"canary bytes")
    case_json = _write_case(
        tmp_path,
        "dataset/case_b",
        {"name": "absolute", "input": {"path": str(canary)}, "expected": {}},
    )
    with pytest.raises(ValueError, match="escapes case_dir"):
        load_case(case_json, backend_id="pymupdf")


def test_resolve_document_path_allows_sibling_file(tmp_path):
    # the documented, intended shape: input.pdf next to case.json
    case_json = _write_case(
        tmp_path,
        "dataset/case_c",
        {"name": "sibling", "input": {"path": "input.pdf"}, "expected": {}},
    )
    (case_json.parent / "input.pdf").write_bytes(b"%PDF-1.4 real doc")
    case = load_case(case_json, backend_id="pymupdf")
    doc = case.request_body["document"]
    assert doc["filename"] == "input.pdf"
    assert base64.b64decode(doc["bytes_base64"]) == b"%PDF-1.4 real doc"


def test_resolve_document_bytes_base64_passthrough(tmp_path):
    inline = base64.b64encode(b"inline pdf bytes").decode()
    case_json = _write_case(
        tmp_path,
        "dataset/case_d",
        {
            "name": "inline_bytes",
            "input": {"bytes_base64": inline, "mime_type": "application/pdf"},
            "expected": {},
        },
    )
    case = load_case(case_json, backend_id="pymupdf")
    doc = case.request_body["document"]
    assert doc == {"bytes_base64": inline, "mime_type": "application/pdf"}


def test_resolve_document_url_passthrough(tmp_path):
    case_json = _write_case(
        tmp_path,
        "dataset/case_e",
        {"name": "remote_doc", "input": {"url": "https://example.com/doc.pdf"}, "expected": {}},
    )
    case = load_case(case_json, backend_id="pymupdf")
    assert case.request_body["document"] == {
        "url": "https://example.com/doc.pdf",
        "mime_type": "application/pdf",
    }


def test_resolve_document_malformed_input_raises_value_error(tmp_path):
    case_json = _write_case(
        tmp_path,
        "dataset/case_f",
        {"name": "malformed", "input": {"nonsense": True}, "expected": {}},
    )
    with pytest.raises(ValueError, match="case input must set"):
        load_case(case_json, backend_id="pymupdf")


def test_load_case_applies_outputs_and_extraction_schema_overrides(tmp_path):
    schema = {"type": "object", "properties": {"total": {"type": "string"}}}
    case_json = _write_case(
        tmp_path,
        "dataset/case_g",
        {
            "name": "typed_extraction",
            "input": {"builtin_sample": True},
            "outputs": {"typed_fields": True, "tables": "cells"},
            "extraction_schema": {"json_schema": schema},
            "expected": {},
        },
    )
    case = load_case(case_json, backend_id="pymupdf")
    assert case.request_body["outputs"] == {"typed_fields": True, "tables": "cells"}
    assert case.request_body["extraction_schema"] == {"json_schema": schema}


# --- BL-112 sub-requirement: load_case forwards a case.json `compliance` key ------------------


def test_load_case_omits_compliance_when_absent():
    # the shipped sample case.json carries no compliance key — request_body must not gain one
    # out of nowhere (this is the structurally-always-None channel BL-112's finding names).
    cases = load_dataset(SAMPLE, backend_id="pymupdf")
    assert "compliance" not in cases[0].request_body


def test_load_case_forwards_compliance_into_request_body(tmp_path):
    case_dir = tmp_path / "case_a"
    case_dir.mkdir()
    case_json = case_dir / "case.json"
    case_json.write_text(
        json.dumps(
            {
                "name": "phi_case",
                "input": {"builtin_sample": True},
                "compliance": {"require_local": True},
                "expected": {},
            }
        )
    )
    case = load_case(case_json, backend_id="pymupdf")
    assert case.request_body["compliance"] == {"require_local": True}


def test_end_to_end_pymupdf_scores_high_on_sample():
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from openreading.adapters.pymupdf import PyMuPDFAdapter

    report = run_dataset(PyMuPDFAdapter(), SAMPLE)
    assert report.errors == 0
    r = report.results[0]
    # born-digital text + table extraction: PyMuPDF recovers both from the sample PDF
    assert r.dimensions["text_contains"] == pytest.approx(1.0)
    assert r.dimensions["table_cell_accuracy"] == pytest.approx(1.0)
    assert r.overall == pytest.approx(1.0)
    assert "mean_overall=1.000" in report.summary()


def test_run_dataset_handles_a_genuinely_unlabeled_case_without_crashing(tmp_path):
    # BL-86/Jin: a "mid-labeling dataset" — one case with a recognized `expected` dimension, one
    # with `expected: {}` (BL-79's honest "not scored" shape, `overall: None`) — must not crash
    # `mean_overall`/`summary()`; the unscored case must be excluded, not silently averaged in.
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from openreading.adapters.pymupdf import PyMuPDFAdapter

    ds = tmp_path / "dataset"
    labeled_dir = ds / "labeled"
    labeled_dir.mkdir(parents=True)
    (labeled_dir / "case.json").write_text(
        json.dumps(
            {
                "name": "labeled",
                "input": {"builtin_sample": True},
                "expected": {"text_contains": ["OpenReading Test Document"]},
            }
        )
    )
    unlabeled_dir = ds / "unlabeled"
    unlabeled_dir.mkdir(parents=True)
    (unlabeled_dir / "case.json").write_text(
        json.dumps({"name": "unlabeled", "input": {"builtin_sample": True}, "expected": {}})
    )

    report = run_dataset(PyMuPDFAdapter(), ds)
    assert report.errors == 0
    by_name = {r.name: r for r in report.results}
    assert by_name["labeled"].overall == pytest.approx(1.0)
    assert by_name["unlabeled"].overall is None  # honest "not scored", never a false 1.0

    # mean_overall must compute over the labeled case alone, not raise TypeError against None
    assert report.mean_overall == pytest.approx(1.0)

    # summary() must not raise either, and must mark the unscored case explicitly rather than
    # feeding None to a ":.3f}" format spec
    text = report.summary()
    assert "mean_overall=1.000" in text
    assert "unlabeled: overall=unscored" in text


# --- compliance gate (BL-121) ------------------------------------------------------------
#
# run_case/run_dataset drive adapter.submit() directly, with no Router in front to apply the
# stage-1 compliance hard-filter — before this fix, a compliance.require_baa=True case against a
# no-BAA backend was submitted and scored (CaseResult.error is None) exactly like a compliant one,
# unlike every other real call site in this codebase (Router.check_eligible / Router.route /
# calibrate_strategy's own BL-112 fix, the pattern mirrored here).


class _CountingBackend(ConfigurableBackend):
    """Counts real submit() calls, so a compliance-refusal test can prove ComplianceRefused fires
    before adapter.submit() is ever reached — not merely that the result ends up unscored."""

    def __init__(self, descriptor):
        super().__init__(descriptor)
        self.submit_calls = 0

    def submit(self, req, ctx):
        self.submit_calls += 1
        return super().submit(req, ctx)


def _hipaa_case() -> EvalCase:
    return EvalCase(
        name="hipaa_case",
        request_body={
            "document": {"bytes_base64": "aGVsbG8=", "mime_type": "application/pdf"},
            "backend": {"id": "phi-backend"},
            "compliance": {"require_baa": True},
        },
        expected={},
    )


def test_run_case_refuses_a_noncompliant_backend_before_submit():
    fake = _CountingBackend(make_backend("phi-backend", hipaa_baa="no").descriptor)

    result = run_case(fake, _hipaa_case())

    assert fake.submit_calls == 0  # refused before any backend call
    assert result.error is not None
    assert "ComplianceRefused" in result.error
    assert "hipaa_baa" in result.error
    assert result.overall == 0.0


def test_run_case_compliant_backend_is_unaffected():
    # Positive control: a backend that DOES carry a BAA runs to completion exactly as before —
    # the gate is purely additive, never narrowing an already-eligible backend.
    fake = _CountingBackend(make_backend("phi-backend", hipaa_baa="yes").descriptor)

    result = run_case(fake, _hipaa_case())

    assert fake.submit_calls == 1
    assert result.error is None


def test_run_case_router_config_confirms_a_tier_gated_baa():
    # The new router_config parameter is actually threaded into comp.evaluate, not merely
    # accepted and ignored: a tier-gated BAA is refused without the operator's confirmation and
    # accepted once the backend id is listed in RouterConfig.baa_tier_confirmed.
    fake = _CountingBackend(make_backend("phi-backend", hipaa_baa="tier_gated").descriptor)

    refused = run_case(fake, _hipaa_case())
    assert refused.error is not None and fake.submit_calls == 0

    cfg = RouterConfig(baa_tier_confirmed=frozenset({"phi-backend"}))
    confirmed = run_case(fake, _hipaa_case(), router_config=cfg)
    assert confirmed.error is None and fake.submit_calls == 1


def test_run_dataset_forwards_router_config_to_every_case(monkeypatch):
    # run_dataset has no policy-union step of its own (unlike calibrate_strategy) — it only needs
    # to thread the caller's router_config through to run_case for every case in the dataset.
    monkeypatch.setattr(
        "openreading.evals.runner.load_dataset",
        lambda dataset_dir, *, backend_id: [_hipaa_case()],
    )
    fake = _CountingBackend(make_backend("phi-backend", hipaa_baa="tier_gated").descriptor)

    refused = run_dataset(fake, "unused")
    assert refused.errors == 1 and fake.submit_calls == 0

    confirmed = run_dataset(
        fake, "unused", router_config=RouterConfig(baa_tier_confirmed=frozenset({"phi-backend"}))
    )
    assert confirmed.errors == 0 and fake.submit_calls == 1
