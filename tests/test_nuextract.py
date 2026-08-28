"""NuExtract adapter — tested against documented-shape fixtures via an injected fake client (NO
network). Fixtures mirror the platform SDK's StructuredExtractionResponse /
ContentExtractionResponse wire shapes (github.com/numindai/nuextract-platform-sdk, accessed
2026-07-27); they upgrade to captured-live the first time `make verify-live` runs with
NUEXTRACT_API_KEY. Exercises the temp-project POLL flow, typed_fields with native JSON values
preserved, the NuMarkdown parse with its deterministic markdown→text projection, and per-job
token-usage reporting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.nuextract import NuExtractAdapter
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType, JobState
from openreading.types.enums import WaitMode
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "nuextract"

# A NuExtract typed template (the platform's OWN format, not JSON Schema) — passed verbatim.
TEMPLATE = {
    "invoice_number": "verbatim-string",
    "total": "number",
    "currency": ["USD", "EUR"],
    "line_items": [{"description": "verbatim-string", "amount": "number"}],
}


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeNuExtractClient:
    """Replays the documented-shape fixtures over the POLL flow: job creation returns a jobId,
    the first status poll is running, later polls completed, then the op-specific jobs endpoint
    serves the fixture. Records the project lifecycle so tests can assert temp-project cleanup."""

    def __init__(self) -> None:
        self.created_projects: list[dict] = []
        self.deleted_projects: list[str] = []
        self._status_calls = 0

    def create_project(self, name, template, instructions):
        self.created_projects.append(
            {"name": name, "template": template, "instructions": instructions}
        )
        return {"id": f"proj_{len(self.created_projects)}"}

    def create_structured_job(self, project_id, input_bytes, *, filename="document.pdf"):
        return {"jobId": "job_extract_1"}

    def create_content_job(self, input_bytes, *, filename="document.pdf"):
        return {"jobId": "job_parse_1"}

    def get_job_status(self, job_id):
        self._status_calls += 1
        return {"status": "completed" if self._status_calls > 1 else "running"}

    def get_structured_result(self, job_id):
        return _fixture("extract")

    def get_content_result(self, job_id):
        return _fixture("parse")

    def delete_project(self, project_id):
        self.deleted_projects.append(project_id)


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": "ZmFrZQ==", "mime_type": "application/pdf"},
        "backend": {"id": "nuextract"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _extract_req(**over) -> OpenReadingRequest:
    return _req(
        backend={"id": "nuextract", "operation": "extract"},
        extraction_schema={"json_schema": TEMPLATE, "instructions": "amounts are USD"},
        **over,
    )


def _run(adapter, req):
    job = adapter.submit(req, RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED
    return adapter.normalize(job, RunContext(), req)


# --- conformance -----------------------------------------------------------------------


def test_nuextract_conforms():
    # Phase B.2: nuextract is remediated — the text channel is a fence-safe plain projection
    # (C1) and every requested N/D channel is delivered-or-warned (C6), so both are promoted
    # from advisory to strict. blocks (X→D) are derived from the NuMarkdown.
    adapter = NuExtractAdapter(client=FakeNuExtractClient())
    check_adapter_conformance(
        adapter,
        [
            ConformanceCase(request=_req(), deterministic=True, label="parse"),
            ConformanceCase(request=_extract_req(), deterministic=True, label="extract"),
        ],
        strict_checks={"C1", "C6"},
        # Ledger T4a (R1/R2/R3): a fresh instance with its own separate fake client — proves
        # resume has no instance-affinity requirement (AC-5) and nothing is cached on self (AC-7).
        adapter_factory=lambda: NuExtractAdapter(client=FakeNuExtractClient()),
    )


# --- temp-project POLL flow ------------------------------------------------------------


def test_submit_returns_running_poll_job():
    adapter = NuExtractAdapter(client=FakeNuExtractClient())
    job = adapter.submit(_extract_req(), RunContext())
    assert job.wait_mode is WaitMode.POLL and job.state is JobState.RUNNING
    assert job.backend_job_id == "job_extract_1"
    assert (job.poll_handle or {})["project_id"] == "proj_1"


def test_extract_creates_then_deletes_temp_project():
    client = FakeNuExtractClient()
    _run(NuExtractAdapter(client=client), _extract_req())
    assert client.created_projects[0]["template"] == TEMPLATE
    assert client.created_projects[0]["instructions"] == "amounts are USD"
    assert client.deleted_projects == ["proj_1"]  # create → run → delete, like the vendor SDK


def test_parse_needs_no_project():
    client = FakeNuExtractClient()
    _run(NuExtractAdapter(client=client), _req())
    assert client.created_projects == [] and client.deleted_projects == []


# --- extract → typed_fields ------------------------------------------------------------


def test_extract_preserves_native_json_values():
    resp = _run(NuExtractAdapter(client=FakeNuExtractClient()), _extract_req())
    assert resp.typed_fields is not None
    assert resp.typed_fields["invoice_number"].value == "INV-042"
    assert resp.typed_fields["total"].value == 4400.0  # a number stays a number
    assert resp.typed_fields["line_items"].value[1]["amount"] == 4000.0  # nesting preserved
    assert resp.usage is not None
    assert resp.usage.input_tokens == 1834 and resp.usage.output_tokens == 96
    assert resp.usage.pages_processed == 2  # documentInfo.partCount


def test_extract_requires_template():
    from openreading.types.errors import TerminalError

    adapter = NuExtractAdapter(client=FakeNuExtractClient())
    req = _req(backend={"id": "nuextract", "operation": "extract"})  # no extraction_schema
    with pytest.raises(TerminalError) as exc:
        adapter.submit(req, RunContext())
    assert exc.value.backend_code == "no_template"


# --- parse (NuMarkdown) → markdown + derived text --------------------------------------


def test_parse_markdown_and_derived_text():
    resp = _run(NuExtractAdapter(client=FakeNuExtractClient()), _req())
    assert resp.document.markdown and "# Loan Application" in resp.document.markdown
    text = resp.document.text or ""
    assert "Loan Application" in text and "#" not in text  # headings stripped
    assert "Jane Doe" in text and "mailto" not in text  # links → label
    assert "ACCT 001-42" in text and "```" not in text  # fence markers out, content kept
    assert "|" not in text and "$4,400.00" in text  # table pipes flattened, cells kept
    # blocks are now DERIVED (X→D) from the NuMarkdown → one synthetic page (§4.3 container rule)
    assert resp.document.pages is not None and len(resp.document.pages) == 1
    assert any(w.code == "page_attribution_unavailable" for w in (resp.warnings or []))


# --- Phase B.2: derive-library adoption + blocks X→D + confidence/page_count + op warnings ------


def _parse(md: str, **out) -> object:
    """Normalize a NuMarkdown `result` string directly (house pattern = test_reducto_normalize)."""
    return NuExtractAdapter()._normalize_parse({"result": md}, Outputs(**out))


def test_md_to_text_preserves_snake_case_star_math_and_fenced_code():
    # The deleted local _md_to_text did a blanket [*_`] deletion that corrupted content and
    # transformed fenced bodies; derive.md_to_text is paired-delimiter + fence-safe.
    md = "Config uses `snake_case` and `total = 3*4`.\n\n```\nx = a_b * c_d  # 3*4 stays\n```\n"
    text = _parse(md).document.text or ""
    assert "snake_case" in text  # underscores survive (not "snakecase")
    assert "3*4" in text  # bare asterisk math survives (not "34")
    assert "a_b * c_d" in text  # fenced body is verbatim (not "ab  cd")
    assert "`" not in text  # inline-code backticks are stripped, content kept


def test_parse_derives_typed_blocks_in_one_synthetic_page():
    resp = _run(NuExtractAdapter(client=FakeNuExtractClient()), _req())
    pages = resp.document.pages
    assert pages is not None and len(pages) == 1 and pages[0].page_number == 1
    blocks = pages[0].blocks or []
    assert any(b.type is BlockType.TITLE and b.text == "Loan Application" for b in blocks)
    assert any(b.type is BlockType.TABLE for b in blocks)
    # bbox-less derived blocks — geometry is NOT fabricated (block_bbox stays X)
    assert all(b.bbox is None for b in blocks)
    assert any(w.code == "page_attribution_unavailable" for w in (resp.warnings or []))


def test_parse_table_block_carries_the_canonical_grid():
    # blocks X→D unlocks the declared-but-unreachable table_cells=D (Table lives on Block.table).
    resp = _run(NuExtractAdapter(client=FakeNuExtractClient()), _req())
    table_blk = next(b for b in resp.document.pages[0].blocks if b.type is BlockType.TABLE)
    assert table_blk.table is not None and table_blk.table.cells
    assert table_blk.table.rows[0] == ["Item", "Amount"]
    assert ["Total", "$4,400.00"] in table_blk.table.rows
    # the block text channel is the plain grid projection (tab-joined), never pipe markup
    assert "|" not in (table_blk.text or "") and "\t" in (table_blk.text or "")


def test_parse_routes_output_token_probability_to_document_confidence():
    resp = _run(NuExtractAdapter(client=FakeNuExtractClient()), _req())
    assert resp.document.confidence == pytest.approx(0.88)  # the provider's only conf signal


def test_parse_channel_provenance_marks_native_vs_derived():
    resp = _run(NuExtractAdapter(client=FakeNuExtractClient()), _req())
    assert resp.channel_provenance == {
        "markdown": "native",
        "text": "derived",
        "blocks": "derived",
        "table_cells": "derived",
    }


def test_extract_sets_page_count_and_document_confidence():
    resp = _run(NuExtractAdapter(client=FakeNuExtractClient()), _extract_req())
    assert resp.document.page_count == 2  # documentInfo.partCount
    assert resp.document.confidence == pytest.approx(0.92)
    assert resp.channel_provenance == {"typed_fields": "native"}


def test_extract_warns_content_channels_absent_for_the_operation():
    # C6 deliver-or-warn: the extract op returns typed fields only; the requested markdown/text/
    # blocks channels are not produced → each is named in a machine-readable warning (never a
    # silent empty).
    resp = _run(NuExtractAdapter(client=FakeNuExtractClient()), _extract_req())
    warned_fields = {w.field for w in (resp.warnings or [])}
    assert {"markdown", "text", "blocks"} <= warned_fields
    assert all(
        w.code == "channel_not_produced_by_operation"
        for w in (resp.warnings or [])
        if w.field in {"markdown", "text", "blocks"}
    )


def test_no_credentials_is_terminal():
    from openreading.types.errors import TerminalError

    adapter = NuExtractAdapter()  # no injected client, no creds in ctx
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "no_credentials"


# --- live (keyed; skipped without NUEXTRACT_API_KEY) -----------------------------------


@pytest.mark.live
def test_live_parse():  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("nuextract")
    resp = run_live("nuextract", NuExtractAdapter(), sample_pdf_request("nuextract"))
    assert resp.document.markdown


@pytest.mark.live
def test_live_extract():  # pragma: no cover
    # F2 (verified 2026-07-29): the extract op end-to-end against the real platform — temp project
    # (POST /api/structured-extraction) → structured job → poll → flatten `result` into
    # typed_fields. The synthetic sample PDF may not contain the field, so assert the typed_fields
    # map is produced (keys present), not a specific value.
    from openreading.types.request import ExtractionSchema
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("nuextract")
    req = sample_pdf_request("nuextract", operation="extract")
    req.extraction_schema = ExtractionSchema(json_schema={"title": "verbatim-string"})
    resp = run_live("nuextract", NuExtractAdapter(), req, capture="live_extract")
    assert resp.typed_fields is not None  # extract op returns a typed_fields map
