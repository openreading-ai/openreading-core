"""Phase 0 (Manifest v0.6) — the new schema families + pydantic mirrors are internally consistent
and round-trip. batch-result.v0.1 and corpus-report.v0.1 follow the Canon §8 response-family rules
(required schema_version const; filename==$id==const==pydantic default is covered by the C12
meta-test); adapter-descriptor v0.4 adds the optional `batch` block additively."""

from __future__ import annotations

import json
from pathlib import Path

import pydantic
import pytest
from jsonschema.exceptions import ValidationError

from openreading import schemas
from openreading.types.batch import (
    MAX_BATCH_JOBS,
    BatchItem,
    BatchRequestEcho,
    BatchResult,
    BatchSummary,
    CorpusReport,
    SourceRef,
)

GOLDEN = Path(__file__).parent / "golden"


def _minimal_response() -> dict:
    return {
        "schema_version": "0.3",
        "status": {"state": "succeeded"},
        "backend": {"id": "pymupdf", "type": "oss_library"},
        "document": {"text": "hello"},
    }


# --- batch-result family ----------------------------------------------------------------


def test_batch_result_schema_const_is_0_2():
    assert schemas.batch_result_schema()["properties"]["schema_version"]["const"] == "0.2"


def test_batch_result_pydantic_round_trips_to_schema_valid():
    br = BatchResult(
        status={"state": "succeeded"},
        items=[
            BatchItem(
                source=SourceRef(
                    filename="a.pdf", format="pdf", relpath="a.pdf", sha256="deadbeef"
                ),
                state="succeeded",
                response=_minimal_response(),
                transport="platform",
            )
        ],
        summary=BatchSummary(total=1, succeeded=1, failed=0),
    )
    doc = br.to_schema_dict()
    assert doc["schema_version"] == "0.2"  # producer default stamped
    schemas.validate_batch_result(doc)


def test_batch_result_failed_items_validate():
    br = BatchResult(
        status={"state": "partial"},
        items=[
            BatchItem(
                source=SourceRef(filename="x.docx", format="docx"),
                state="failed",
                error={"code": "unsupported_format", "message": "backend cannot read docx"},
            ),
            BatchItem(
                source=SourceRef(filename="y.pdf", format="pdf"),
                state="failed",
                error={"code": "backend_error", "message": "boom"},
            ),
        ],
        summary=BatchSummary(total=2, succeeded=0, failed=2, cost_bases=["estimated"]),
    )
    schemas.validate_batch_result(br.to_schema_dict())


def test_batch_result_rejects_bad_state():
    bad = {
        "schema_version": "0.2",
        "status": {"state": "done"},  # not in the closed enum
        "items": [],
        "summary": {"total": 0, "succeeded": 0, "failed": 0},
    }
    with pytest.raises(ValidationError):
        schemas.validate_batch_result(bad)


def test_batch_request_echo_jobs_structural_bound_rejects_non_positive():
    # BL-84: a structural backstop alongside the procedural bound_jobs() helper — this model must
    # never represent a `jobs` value that would fail the schema's own "minimum": 1.
    with pytest.raises(pydantic.ValidationError):
        BatchRequestEcho(jobs=0)


def test_batch_request_echo_jobs_has_no_structural_ceiling_the_procedural_check_owns_that():
    # BL-94: the structural Field(le=MAX_BATCH_JOBS) this test used to assert collided with the
    # procedural bound_jobs() ceiling override — a caller-raised max_jobs (--max-jobs / max_jobs=)
    # could never be represented by this model even when bound_jobs() correctly permitted the
    # value through, crashing the entire useful input range of that escape hatch. The schema this
    # model mirrors imposes no maximum at all ("jobs": {"minimum": 1}, no "maximum"), so the
    # model now enforces only that floor; the ceiling is bound_jobs()'s alone.
    assert BatchRequestEcho(jobs=MAX_BATCH_JOBS + 1).jobs == MAX_BATCH_JOBS + 1
    assert BatchRequestEcho(jobs=10_000).jobs == 10_000  # no structural ceiling at all any more


def test_batch_request_echo_jobs_structural_bound_accepts_the_valid_range():
    assert BatchRequestEcho(jobs=1).jobs == 1
    assert BatchRequestEcho(jobs=MAX_BATCH_JOBS).jobs == MAX_BATCH_JOBS
    assert BatchRequestEcho().jobs is None  # unset stays unset — the field is still optional


def test_batch_result_envelope_is_forward_tolerant():
    # unknown top-level field parses (extra="ignore" on the envelope) and is dropped on re-serialize
    obj = BatchResult.model_validate(
        {
            "schema_version": "0.2",
            "status": {"state": "succeeded"},
            "items": [],
            "summary": {"total": 0, "succeeded": 0, "failed": 0},
            "some_future_field": {"x": 1},
        }
    )
    out = obj.to_schema_dict()
    assert "some_future_field" not in out
    schemas.validate_batch_result(out)


# --- corpus-report family ---------------------------------------------------------------


def test_corpus_report_schema_const_is_0_1():
    assert schemas.corpus_report_schema()["properties"]["schema_version"]["const"] == "0.1"


def test_corpus_report_pydantic_round_trips_to_schema_valid():
    cr = CorpusReport(
        subjects=[{"label": "reducto", "source": "runA.json", "backend_tally": {"reducto": 2}}],
        documents=[
            {
                "source": {"relpath": "a.pdf", "sha256": "d1"},
                "verdict": "equivalent",
                "report": None,
            },
            {"source": {"relpath": "b.pdf", "sha256": "d2"}, "verdict": "unpaired", "report": None},
        ],
        rollup={"documents": 2, "equivalent": 1, "divergent": 0, "mixed": 0, "unpaired": 1},
    )
    schemas.validate_corpus_report(cr.to_schema_dict())


def test_corpus_report_rejects_bad_verdict():
    bad = {
        "schema_version": "0.2",
        "subjects": [],
        "documents": [{"source": {"relpath": "a.pdf"}, "verdict": "kinda"}],
        "rollup": {"documents": 1},
    }
    with pytest.raises(ValidationError):
        schemas.validate_corpus_report(bad)


# --- adapter-descriptor v0.4 (additive batch block) -------------------------------------


# --- golden fixtures for the new families ----------------------------------------------


def test_golden_batch_result_validates():
    """v0.1 stays on disk frozen, and no longer validates against the current schema: it carries a
    `skipped` item, which the vocabulary this release removed. v0.2 is the same run recorded after
    intake started dispatching every source, so the svg is a failed item with the backend's own
    reason."""
    schemas.validate_batch_result(json.loads((GOLDEN / "batch-result" / "v0.2.json").read_text()))


def test_golden_corpus_report_validates():
    schemas.validate_corpus_report(json.loads((GOLDEN / "corpus-report" / "v0.1.json").read_text()))
