"""LlamaParse v2 tier adapters: pinned request contract, POLL lifecycle, and honest normalization.

Every response here is a hand-written fixture shaped from the `llama-cloud` 2.16.0 SDK models
(`ParsingCreateResponse`, `ParsingGetResponse` and the item types), read on 2026-09-13. No test
calls the service, so passing proves compatibility with those documented shapes, not live
behaviour. The HTTP cases replay the SDK's own wire choices through respx: multipart
`configuration`, bearer auth, and repeated `expand` query parameters.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from openreading.adapters.llamaparse import (
    TIERS,
    LlamaParseAgenticAdapter,
    LlamaParseAgenticPlusAdapter,
    LlamaParseCostEffectiveAdapter,
    LlamaParseFastAdapter,
)
from openreading.adapters.llamaparse.adapter import HttpxLlamaParseClient
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType, JobState
from openreading.types.enums import WaitMode
from openreading.types.errors import RetryableError, TerminalError, UnsupportedFeatureError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import ResolvedCredentials, RunContext

FIXTURES = Path(__file__).parent / "fixtures"
PDF = b"%PDF-1.7\n%synthetic\n"
ADAPTERS = {
    "fast": LlamaParseFastAdapter,
    "cost_effective": LlamaParseCostEffectiveAdapter,
    "agentic": LlamaParseAgenticAdapter,
    "agentic_plus": LlamaParseAgenticPlusAdapter,
}
PINNED = {
    "fast": "2026-06-15",
    "cost_effective": "2026-08-19",
    "agentic": "2026-09-07",
    "agentic_plus": "2026-08-19",
}


def _fixture(tier: str) -> dict:
    name = "llamaparse-fast" if tier == "fast" else "llamaparse-agentic"
    data = json.loads((FIXTURES / name / "parse.json").read_text())
    data["job"]["tier"] = tier
    return data


class FakeClient:
    def __init__(self, tier="agentic", statuses=("PENDING", "RUNNING"), final=None, error=None):
        self.final = final if final is not None else _fixture(tier)
        self.statuses = list(statuses)
        self.error = error
        self.uploads: list[dict] = []
        self.creates: list[dict] = []
        self.gets: list[tuple[str, list[str]]] = []
        self.cancels: list[str] = []

    def upload(self, filename, data, mime_type, configuration):
        if self.error is not None:
            raise self.error
        self.uploads.append(
            {
                "filename": filename,
                "size": len(data),
                "mime_type": mime_type,
                "configuration": configuration,
            }
        )
        return {"id": "pjb_1", "project_id": "prj_fixture", "status": "PENDING"}

    def create(self, body):
        if self.error is not None:
            raise self.error
        self.creates.append(body)
        return {"id": "pjb_1", "project_id": "prj_fixture", "status": "PENDING"}

    def get(self, job_id, expand):
        self.gets.append((job_id, list(expand)))
        if self.statuses:
            return {
                "job": {"id": job_id, "project_id": "prj_fixture", "status": self.statuses.pop(0)}
            }
        return copy.deepcopy(self.final)

    def cancel(self, job_id):
        self.cancels.append(job_id)
        return {"id": job_id, "project_id": "prj_fixture", "status": "CANCELLED"}


def _slug(tier: str) -> str:
    return "llamaparse-" + tier.replace("_", "-")


def _req(tier="agentic", data=PDF, **over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": base64.b64encode(data).decode(), "filename": "invoice.pdf"},
        "backend": {"id": _slug(tier)},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter, req):
    job = adapter.submit(req, RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED
    return job, adapter.normalize(job, RunContext(), req)


@pytest.mark.parametrize("tier", sorted(ADAPTERS))
def test_each_tier_conforms(tier):
    cls = ADAPTERS[tier]
    cases = [ConformanceCase(request=_req(tier), label="parse")]
    if tier != "fast":
        cases.append(
            ConformanceCase(request=_req(tier, outputs={"tables": "cells"}), label="cells")
        )
    check_adapter_conformance(
        cls(client=FakeClient(tier)),
        cases,
        adapter_factory=lambda: cls(client=FakeClient(tier)),
    )


@pytest.mark.parametrize("tier", sorted(ADAPTERS))
def test_upload_sends_pinned_tier_version_and_nothing_that_adds_cost(tier):
    client = FakeClient(tier)
    job = ADAPTERS[tier](client=client).submit(_req(tier), RunContext())
    assert job.wait_mode is WaitMode.POLL and job.state is JobState.RUNNING
    assert job.backend_job_id == "pjb_1"
    upload = client.uploads[0]
    assert (upload["filename"], upload["size"], upload["mime_type"]) == (
        "invoice.pdf",
        len(PDF),
        "application/pdf",
    )
    expected = {"tier": tier, "version": PINNED[tier]}
    if tier in {"agentic", "agentic_plus"}:
        expected["processing_options"] = {"cost_optimizer": {"enable": False}}
    assert upload["configuration"] == expected
    assert TIERS[tier].version == PINNED[tier]


def test_url_documents_use_the_json_endpoint_with_the_same_configuration():
    client = FakeClient()
    request = OpenReadingRequest.model_validate(
        {
            "document": {"url": "https://example.invalid/a.pdf"},
            "backend": {"id": "llamaparse-agentic"},
        }
    )
    LlamaParseAgenticAdapter(client=client).submit(request, RunContext())
    assert client.uploads == []
    assert client.creates == [
        {
            "source_url": "https://example.invalid/a.pdf",
            "tier": "agentic",
            "version": "2026-09-07",
            "processing_options": {"cost_optimizer": {"enable": False}},
        }
    ]


def test_a_dated_version_override_is_forwarded_and_reported():
    client = FakeClient("cost_effective")
    adapter = LlamaParseCostEffectiveAdapter(client=client)
    request = _req(
        "cost_effective", backend={"id": "llamaparse-cost-effective", "version": "2026-06-26"}
    )
    _job, response = _run(adapter, request)
    assert client.uploads[0]["configuration"]["version"] == "2026-06-26"
    assert response.backend.version == "2026-06-26"


@pytest.mark.parametrize("version", ["latest", "configured", "2026-9-7", "2026-09-07; drop"])
def test_unpinned_or_malformed_versions_are_refused_before_upload(version):
    client = FakeClient()
    request = _req(backend={"id": "llamaparse-agentic", "version": version})
    with pytest.raises(TerminalError) as error:
        LlamaParseAgenticAdapter(client=client).submit(request, RunContext())
    assert error.value.backend_code == "version_unpinned"
    assert client.uploads == []


@pytest.mark.parametrize(
    "over,kind",
    [
        (
            {"document": {"bytes_base64": base64.b64encode(PDF).decode(), "password": "secret"}},
            TerminalError,
        ),
        ({"document": {"file_id": "file_1"}}, TerminalError),
        ({"pages": {"max_pages": 1}}, UnsupportedFeatureError),
        ({"features": {"ocr": "off"}}, UnsupportedFeatureError),
        ({"features": {"ocr": "force"}}, UnsupportedFeatureError),
        ({"features": {"ocr_languages": ["fra"]}}, UnsupportedFeatureError),
        (
            {"async": {"mode": "async", "webhook_url": "https://example.invalid/hook"}},
            UnsupportedFeatureError,
        ),
    ],
)
def test_unforwarded_request_options_are_refused_not_ignored(over, kind):
    client = FakeClient()
    with pytest.raises(kind) as error:
        LlamaParseAgenticAdapter(client=client).submit(_req(**over), RunContext())
    assert "secret" not in str(error.value)
    assert client.uploads == [] and client.creates == []


@pytest.mark.parametrize("tier", sorted(ADAPTERS))
def test_poll_waits_for_completion_and_expands_only_what_the_tier_returns(tier):
    client = FakeClient(tier)
    _run(ADAPTERS[tier](client=client), _req(tier))
    expands = {tuple(expand) for _job_id, expand in client.gets}
    if tier == "fast":
        assert expands == {("text", "metadata", "usage")}
    else:
        assert expands == {("text", "markdown", "items", "metadata", "usage")}
    assert len(client.gets) == 3


@pytest.mark.parametrize("status,code", [("FAILED", "job_failed"), ("CANCELLED", "job_cancelled")])
def test_terminal_vendor_states_raise_without_echoing_vendor_text(status, code):
    final = {
        "job": {
            "id": "pjb_1",
            "project_id": "p",
            "status": status,
            "error_message": "customer SSN 123",
        }
    }
    adapter = LlamaParseAgenticAdapter(client=FakeClient(statuses=(), final=final))
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(TerminalError) as error:
        adapter.poll(job, RunContext())
    assert error.value.backend_code == code
    assert "123" not in str(error.value)


def test_agentic_items_become_blocks_without_boxes_or_inferred_headers():
    job, response = _run(
        LlamaParseAgenticAdapter(client=FakeClient()), _req(outputs={"tables": "cells"})
    )
    first, second = response.document.pages
    assert (first.width, first.height, first.unit.value, first.confidence) == (
        612.0,
        792.0,
        "pdf_point",
        0.93,
    )
    assert first.text.startswith("ACME\nInvoice") and first.markdown.startswith("# Invoice")
    assert [b.type for b in first.blocks] == [
        BlockType.HEADER,
        BlockType.TEXT,
        BlockType.SECTION_HEADER,
        BlockType.TEXT,
        BlockType.TABLE,
        BlockType.FIGURE,
        BlockType.FOOTER,
        BlockType.TEXT,
    ]
    header = first.blocks[0]
    assert (header.text, header.children) == (None, [first.blocks[1].id])
    assert first.blocks[1].text == "ACME"
    assert first.blocks[5].text == "Company logo"
    assert [b.reading_order for b in first.blocks] == list(range(8))
    assert all(block.bbox is None and block.confidence is None for block in first.blocks)
    table = first.blocks[4].table
    assert first.blocks[4].text == "Name\tQty\nWidget\t2\n\t2.5"
    assert table.rows == [["Name", "Qty"], ["Widget", "2"], [None, "2.5"]]
    assert (table.n_rows, table.n_cols) == (3, 2)
    assert all(cell.is_header is None for cell in table.cells)
    assert [b.type for b in second.blocks] == [
        BlockType.SECTION_HEADER,
        BlockType.LIST,
        BlockType.LIST_ITEM,
        BlockType.CODE,
        BlockType.OTHER,
    ]
    assert second.blocks[1].children == [second.blocks[2].id]
    assert second.blocks[2].text == "Payable within 45 days."
    assert (second.blocks[4].native_type, second.blocks[4].text) == ("link", "terms")
    assert {w.code: w.field for w in response.warnings}["block_bbox_unavailable"] == "block_bbox"
    assert (response.usage.credits, response.usage.pages_processed) == (45.0, 2)
    assert response.backend.version == "2026-09-07"
    report = LlamaParseAgenticAdapter(client=FakeClient()).report_cost(job)
    assert (report.native_unit, report.native_quantity) == ("credit", 45.0)


def test_a_failed_page_makes_the_response_partial():
    final = _fixture("agentic")
    final["items"]["pages"][1] = {"page_number": 2, "success": False, "error": "page timeout"}
    final["markdown"]["pages"][1] = {"page_number": 2, "success": False, "error": "page timeout"}
    _job, response = _run(LlamaParseAgenticAdapter(client=FakeClient(final=final)), _req())
    assert response.status.state.value == "partial"
    assert any(w.code == "partial_conversion" for w in response.warnings)
    assert response.document.pages[1].blocks is None
    assert "timeout" not in json.dumps([w.message for w in response.warnings])


def test_fast_tier_returns_text_and_names_every_missing_structure():
    _job, response = _run(
        LlamaParseFastAdapter(client=FakeClient("fast")), _req("fast", outputs={"tables": "cells"})
    )
    page = response.document.pages[0]
    assert page.text == "Invoice\nInvoice total 1,250.00"
    assert (page.markdown, page.blocks, page.width, page.confidence) == (None, None, None, None)
    codes = {w.code: w.field for w in response.warnings}
    assert codes["markdown_unavailable"] == "markdown"
    assert codes["blocks_unavailable"] == "blocks"
    assert codes["table_cells_unavailable"] == "table_cells"


def test_missing_credits_fall_back_to_pages_rather_than_a_guess():
    final = _fixture("agentic")
    final["job"]["usage"] = {"credits": None}
    job, response = _run(LlamaParseAgenticAdapter(client=FakeClient(final=final)), _req())
    assert response.usage.credits is None
    report = LlamaParseAgenticAdapter().report_cost(job)
    assert (report.native_unit, report.native_quantity) == ("page", 2.0)


@respx.mock
@pytest.mark.parametrize("tier", sorted(ADAPTERS))
def test_billed_credits_are_requested_over_http_and_reach_both_reports(tier):
    respx.post(f"{API}/api/v2/parse/upload").respond(200, json={"id": "pjb_1", "status": "PENDING"})

    def expanded_response(request):
        final = _fixture(tier)
        final["job"].pop("usage", None)
        if "usage" in request.url.params.get_list("expand"):
            final["job"]["usage"] = {"credits": 12.5}
        return httpx.Response(200, json=final)

    respx.get(f"{API}/api/v2/parse/pjb_1").mock(side_effect=expanded_response)
    adapter = ADAPTERS[tier](client=HttpxLlamaParseClient("test-key", API))
    job, response = _run(adapter, _req(tier))
    assert response.usage.credits == 12.5
    cost = adapter.report_cost(job)
    assert (cost.native_unit, cost.native_quantity) == ("credit", 12.5)


def test_disabling_table_output_keeps_table_text_without_cells_or_cell_provenance():
    _job, response = _run(
        LlamaParseAgenticAdapter(client=FakeClient()), _req(outputs={"tables": "none"})
    )
    tables = [b for p in response.document.pages for b in p.blocks if b.type is BlockType.TABLE]
    assert tables and tables[0].text == "Name\tQty\nWidget\t2\n\t2.5"
    assert all(block.table is None for block in tables)
    assert "table_cells" not in response.channel_provenance
    assert not any(w.code == "table_cells_unavailable" for w in response.warnings)


def test_cancel_calls_the_vendor_once_and_skips_finished_jobs():
    client = FakeClient()
    adapter = LlamaParseAgenticAdapter(client=client)
    job = adapter.submit(_req(), RunContext())
    assert adapter.cancel(job, RunContext()).state is JobState.CANCELLED
    adapter.cancel(job, RunContext())
    assert client.cancels == ["pjb_1"]


def test_a_rejected_cancel_is_raised_as_mapped():
    class RejectingCancel(FakeClient):
        def cancel(self, job_id):
            raise httpx.ConnectError("offline")

    adapter = LlamaParseAgenticAdapter(client=RejectingCancel())
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(TerminalError):
        adapter.cancel(job, RunContext())


def test_submit_failures_are_mapped_without_vendor_text():
    adapter = LlamaParseAgenticAdapter(client=FakeClient(error=ValueError("customer SSN 123")))
    with pytest.raises(TerminalError) as error:
        adapter.submit(_req(), RunContext())
    assert error.value.backend_code == "request_failed"
    assert "123" not in str(error.value)
    retry = RetryableError("busy", backend_code="http_429")
    with pytest.raises(RetryableError):
        LlamaParseAgenticAdapter(client=FakeClient(error=retry)).submit(_req(), RunContext())


def test_missing_key_is_a_named_credentials_error():
    with pytest.raises(TerminalError) as error:
        LlamaParseAgenticAdapter().submit(_req(), RunContext())
    assert error.value.backend_code == "no_credentials"


def test_health_names_the_shared_install_extra(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_httpx(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_httpx)
    health = LlamaParseFastAdapter().health()
    assert not health.ready
    assert health.missing_deps == ["httpx (pip install 'openreading[llamaparse]')"]


@pytest.mark.parametrize("tier", sorted(ADAPTERS))
def test_descriptors_describe_each_tier_without_claiming_verification(tier):
    descriptor = ADAPTERS[tier]().descriptor
    assert descriptor.id == _slug(tier)
    assert descriptor.type.value == "hosted_api"
    assert descriptor.runtime.version_pin == f"api v2 tier={tier} version={PINNED[tier]}"
    assert descriptor.capabilities.ocr == (
        "claimed" if tier in {"agentic", "agentic_plus"} else False
    )
    assert "verified" not in json.dumps(descriptor.capabilities.model_dump())
    assert [(c.key, c.env) for c in descriptor.credentials_spec] == [
        ("api_key", ["LLAMA_CLOUD_API_KEY", "LLAMA_PARSE_API_KEY"])
    ]
    assert [(c.key, c.env) for c in descriptor.config_spec] == [
        ("base_url", ["LLAMA_CLOUD_BASE_URL"])
    ]
    assert (
        descriptor.accepts_url,
        descriptor.idempotency_supported,
        descriptor.cancel_supported,
    ) == (
        True,
        False,
        True,
    )
    channels = descriptor.output.channels
    structured = "X" if tier == "fast" else "N"
    assert (channels.text.value, channels.markdown.value, channels.blocks.value) == (
        "N",
        structured,
        structured,
    )
    assert (channels.table_cells.value, channels.block_bbox.value, channels.typed_fields.value) == (
        structured,
        "X",
        "X",
    )


API = "https://api.cloud.llamaindex.ai"


@respx.mock
def test_http_client_pins_the_v2_wire_contract():
    upload = respx.post(f"{API}/api/v2/parse/upload").mock(
        return_value=httpx.Response(
            200, json={"id": "pjb_1", "project_id": "p", "status": "PENDING"}
        )
    )
    create = respx.post(f"{API}/api/v2/parse").mock(
        return_value=httpx.Response(
            200, json={"id": "pjb_2", "project_id": "p", "status": "PENDING"}
        )
    )
    get = respx.get(f"{API}/api/v2/parse/pjb_1").mock(
        return_value=httpx.Response(200, json=_fixture("agentic"))
    )
    cancel = respx.post(f"{API}/api/v2/parse/pjb_1/cancel").mock(
        return_value=httpx.Response(
            200, json={"id": "pjb_1", "project_id": "p", "status": "CANCELLED"}
        )
    )
    client = HttpxLlamaParseClient("llx-test-key", API + "/")
    configuration = {"tier": "fast", "version": "2026-06-15"}
    assert client.upload("invoice.pdf", PDF, "application/pdf", configuration)["id"] == "pjb_1"
    sent = upload.calls.last.request
    assert sent.headers["authorization"] == "Bearer llx-test-key"
    body = sent.content
    assert b'name="configuration"' in body and json.dumps(configuration).encode() in body
    assert b'name="file"; filename="invoice.pdf"' in body and PDF in body
    assert (
        client.create({"source_url": "https://example.invalid/a.pdf", **configuration})["id"]
        == "pjb_2"
    )
    assert (
        json.loads(create.calls.last.request.content)["source_url"]
        == "https://example.invalid/a.pdf"
    )
    client.get("pjb_1", ["text", "markdown"])
    query = parse_qs(urlsplit(str(get.calls.last.request.url)).query)
    assert query == {"expand": ["text", "markdown"]}
    assert client.cancel("pjb_1")["status"] == "CANCELLED"
    assert cancel.called


@respx.mock
@pytest.mark.parametrize(
    "status,kind,code",
    [
        (401, TerminalError, "auth_rejected"),
        (429, RetryableError, "http_429"),
        (404, TerminalError, "http_404"),
    ],
)
def test_http_client_maps_status_codes_without_echoing_bodies(status, kind, code):
    respx.get(f"{API}/api/v2/parse/pjb_1").mock(
        return_value=httpx.Response(
            status, json={"detail": "document text 123"}, headers={"Retry-After": "7"}
        )
    )
    with pytest.raises(kind) as error:
        HttpxLlamaParseClient("llx-test-key", API).get("pjb_1", ["text"])
    assert error.value.backend_code == code
    assert "123" not in str(error.value) and "llx-test-key" not in str(error.value)
    if kind is RetryableError:
        assert error.value.retry_after == 7.0


def test_credentials_and_base_url_reach_the_real_client(monkeypatch):
    from openreading.adapters.llamaparse import adapter as module

    built = []
    monkeypatch.setattr(
        module, "HttpxLlamaParseClient", lambda key, url: built.append((key, url)) or FakeClient()
    )
    ctx = RunContext(
        credentials=ResolvedCredentials(values={"api_key": "llx-key"}),
        runtime={"base_url": "https://eu.example.invalid"},
    )
    LlamaParseAgenticAdapter().submit(_req(), ctx)
    LlamaParseAgenticAdapter().submit(
        _req(), RunContext(credentials=ResolvedCredentials(values={"api_key": "k"}))
    )
    assert built == [("llx-key", "https://eu.example.invalid"), ("k", API)]


# --- faults the happy path never reaches ----------------------------------------------------------


def test_an_unknown_job_status_is_terminal_rather_than_polled_forever():
    final = {"job": {"id": "pjb_1", "project_id": "p", "status": "ARCHIVED"}}
    adapter = LlamaParseAgenticAdapter(client=FakeClient(statuses=(), final=final))
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(TerminalError) as error:
        adapter.poll(job, RunContext())
    assert error.value.backend_code == "unknown_status"


def test_status_transport_failures_are_mapped_and_retryable_errors_pass_through():
    class Broken(FakeClient):
        def __init__(self, error):
            super().__init__()
            self.get_error = error

        def get(self, job_id, expand):
            raise self.get_error

    adapter = LlamaParseAgenticAdapter(client=Broken(ValueError("document text 123")))
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(TerminalError) as error:
        adapter.poll(job, RunContext())
    assert error.value.backend_code == "request_failed" and "123" not in str(error.value)
    retry = RetryableError("slow down", backend_code="http_429")
    adapter = LlamaParseAgenticAdapter(client=Broken(retry))
    with pytest.raises(RetryableError):
        adapter.poll(adapter.submit(_req(), RunContext()), RunContext())


def test_a_create_response_without_a_job_id_is_refused():
    class NoId(FakeClient):
        def upload(self, filename, data, mime_type, configuration):
            return {"status": "PENDING"}

    with pytest.raises(TerminalError) as error:
        LlamaParseAgenticAdapter(client=NoId()).submit(_req(), RunContext())
    assert error.value.backend_code == "request_failed"


@pytest.mark.parametrize("document", ["missing_path", "empty_bytes", "bad_base64"])
def test_unreadable_local_documents_are_refused_before_upload(tmp_path, document):
    body = {
        "missing_path": {"path": str(tmp_path / "absent.pdf")},
        "empty_bytes": {"bytes_base64": ""},
        "bad_base64": {"bytes_base64": "not base64!"},
    }[document]
    client = FakeClient()
    request = OpenReadingRequest.model_validate(
        {"document": body, "backend": {"id": "llamaparse-agentic"}}
    )
    with pytest.raises(TerminalError) as error:
        LlamaParseAgenticAdapter(client=client).submit(request, RunContext())
    assert error.value.backend_code == "unsupported_input"
    assert client.uploads == []


def test_a_local_path_uploads_under_its_own_name(tmp_path):
    source = tmp_path / "statement.pdf"
    source.write_bytes(PDF)
    client = FakeClient()
    request = OpenReadingRequest.model_validate(
        {"document": {"path": str(source)}, "backend": {"id": "llamaparse-agentic"}}
    )
    LlamaParseAgenticAdapter(client=client).submit(request, RunContext())
    assert (client.uploads[0]["filename"], client.uploads[0]["size"]) == ("statement.pdf", len(PDF))


def test_a_retryable_cancel_failure_is_raised_unchanged():
    class Busy(FakeClient):
        def cancel(self, job_id):
            raise RetryableError("busy", backend_code="http_503")

    adapter = LlamaParseAgenticAdapter(client=Busy())
    job = adapter.submit(_req(), RunContext())
    with pytest.raises(RetryableError):
        adapter.cancel(job, RunContext())


def test_disabled_outputs_and_an_empty_result_are_named_not_fabricated():
    final = {"job": {"id": "pjb_1", "project_id": "p", "status": "COMPLETED"}}
    _job, response = _run(
        LlamaParseAgenticAdapter(client=FakeClient(final=final)),
        _req(outputs={"include_backend_raw": False}),
    )
    codes = {w.code for w in response.warnings}
    assert {"text_unavailable", "markdown_unavailable", "blocks_unavailable"} <= codes
    assert response.document.pages == [] and response.backend_raw is None
