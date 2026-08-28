"""Anthropic Claude adapter — REAL captured Messages responses replayed via an injected fake
client (NO API calls). Exercises: native-PDF parse → markdown/text (token-stream), tool-use
structured extraction → typed_fields, the never-fabricate-blocks warning, and error mapping.
deterministic=False (LLM generation)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openreading.adapters.anthropic_claude import AnthropicClaudeAdapter
from openreading.ledger.header import slim_request
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType
from openreading.types.enums import ResponseState
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "anthropic-claude"
PDF_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeClaudeClient:
    def __init__(self, fixture="parse", raise_exc=None):
        self._fixture = fixture
        self._raise = raise_exc
        self.last_call = None

    def create(self, *, model, max_tokens, messages, tools=None, tool_choice=None):
        if self._raise:
            raise self._raise
        self.last_call = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        # Mode-aware default: a forced tool call (extraction) returns the tool_use fixture, a plain
        # request returns the token-stream fixture — mirroring the real API. An explicitly named
        # fixture is always served as-is (used by the mode-specific tests below).
        name = self._fixture
        if name == "parse" and tool_choice is not None:
            name = "extract"
        return _fixture(name)


class RateLimitError(Exception):
    pass


class BadRequestError(Exception):
    pass


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": PDF_B64, "mime_type": "application/pdf"},
        "backend": {"id": "anthropic-claude", "type": "hosted_api"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter, req):
    ctx = RunContext()
    # matches every real production call site: normalize() only ever receives the slimmed request
    # (ledger.header.slim_request), never the caller's original `req` with real bytes intact.
    return adapter.normalize(adapter.submit(req, ctx), ctx, slim_request(req))


def test_claude_conforms_non_deterministic():
    # remediated: C1 (text is plain), C6 (deliver-or-warn), C10 (truncation honesty) are strict.
    # Both modes: parse (markdown/text/blocks populated) and extract (typed_fields; the requested
    # prose/block channels are named in warnings, never silently dropped — C6).
    extract_req = _req(
        extraction_schema={
            "json_schema": {"type": "object", "properties": {"gross_pay": {"type": "string"}}}
        }
    )
    check_adapter_conformance(
        AnthropicClaudeAdapter(client=FakeClaudeClient()),
        [
            ConformanceCase(request=_req(), deterministic=False, label="parse"),
            ConformanceCase(
                request=extract_req,
                ctx=RunContext(),
                deterministic=False,
                label="extract",
            ),
        ],
        # Ledger T4a (R1/R2/R3): a fresh instance with its own separate fake client — proves
        # resume has no instance-affinity requirement (AC-5) and nothing is cached on self (AC-7).
        adapter_factory=lambda: AnthropicClaudeAdapter(client=FakeClaudeClient()),
        strict_checks={"C1", "C6", "C10"},
    )


def test_parse_markdown_is_native_and_text_is_a_plain_projection():
    adapter = AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="parse"))
    resp = _run(adapter, _req())
    # markdown channel keeps the model's NATIVE markdown verbatim
    assert resp.document.markdown.startswith("# Loan Application")
    assert "| Region | Revenue |" in resp.document.markdown
    # C1/C2: document.text is a PLAIN projection (md_to_text) — never the markdown verbatim
    assert resp.document.text != resp.document.markdown
    assert "# Loan Application" not in resp.document.text  # heading syntax stripped
    assert "| ---" not in resp.document.text and "|" not in resp.document.text  # no pipe table
    assert "Loan Application" in resp.document.text  # content survives
    assert "Applicant income summary." in resp.document.text
    # table content reaches the plain text channel (tab-joined), C2
    assert "Region\tRevenue" in resp.document.text and "North\t4400" in resp.document.text
    # native PDF: a document block is sent, citations enabled in parse mode
    call = adapter._client.last_call
    doc_block = call["messages"][0]["content"][0]
    assert (
        doc_block["type"] == "document" and doc_block["source"]["media_type"] == "application/pdf"
    )
    assert doc_block["citations"] == {"enabled": True}
    # page_location citations surface as a page count (heuristic; fake PDF bytes don't parse)
    assert resp.document.page_count == 1
    assert resp.backend_raw.object_class == "anthropic.Message"
    # provenance: markdown native, text derived (§3.3/§6.4)
    assert resp.channel_provenance["markdown"] == "native"
    assert resp.channel_provenance["text"] == "derived"


def test_blocks_derived_from_markdown_in_synthetic_page_no_geometry():
    resp = _run(AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="parse")), _req())
    # §4.3 container rule: markdown-derived blocks live in ONE synthetic Page(page_number=1)
    assert resp.document.pages is not None and len(resp.document.pages) == 1
    page = resp.document.pages[0]
    assert page.page_number == 1
    blocks = page.blocks
    assert blocks and len(blocks) == 3
    # h1 → TITLE, para → TEXT, pipe group → TABLE (with the canonical grid)
    assert blocks[0].type is BlockType.TITLE and blocks[0].text == "Loan Application"
    assert blocks[1].type is BlockType.TEXT and blocks[1].text == "Applicant income summary."
    assert blocks[2].type is BlockType.TABLE
    # table_cells (D): cells come from md_table_to_table inside md_to_blocks
    assert blocks[2].table is not None
    assert blocks[2].table.rows == [["Region", "Revenue"], ["North", "4400"]]
    assert blocks[2].table.cells  # a real cell grid, not just rows
    # block_bbox stays X: NO fabricated geometry on any derived block
    assert all(b.bbox is None for b in blocks)
    # a machine-readable warning distinguishes the synthetic container from a real 1-page doc
    assert any(
        w.code == "page_attribution_unavailable" and w.field == "blocks" for w in resp.warnings
    )
    # provenance for the derived structural channels
    assert resp.channel_provenance["blocks"] == "derived"
    assert resp.channel_provenance["table_cells"] == "derived"


def test_max_tokens_stop_reason_yields_partial_not_bare_succeeded():
    # C10 status.honest: a truncation signal must not normalize as a bare SUCCEEDED
    resp = _run(AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="parse_truncated")), _req())
    assert resp.status.state is ResponseState.PARTIAL
    assert any(w.code == "output_truncated" for w in resp.warnings)


def test_end_turn_stop_reason_is_succeeded():
    resp = _run(AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="parse")), _req())
    assert resp.status.state is ResponseState.SUCCEEDED
    assert not any(w.code == "output_truncated" for w in (resp.warnings or []))


def test_extraction_schema_uses_forced_tool_use():
    adapter = AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="extract"))
    req = _req(
        extraction_schema={
            "json_schema": {"type": "object", "properties": {"gross_pay": {"type": "string"}}},
            "instructions": "Pull payroll fields.",
        }
    )
    ctx = RunContext()
    resp = adapter.normalize(adapter.submit(req, ctx), ctx, req)
    # forced a tool whose input_schema is the requested schema
    call = adapter._client.last_call
    assert call["tool_choice"] == {"type": "tool", "name": "extract_fields"}
    assert call["tools"][0]["input_schema"]["properties"] == {"gross_pay": {"type": "string"}}
    # tool_use.input -> typed_fields
    assert resp.typed_fields["borrower_name"].value == "Jane Doe"
    assert resp.typed_fields["gross_pay"].value == "4400.00"


def test_extract_mode_names_absent_content_channels_c6():
    # C6 deliver-or-warn: tool-use extraction yields typed_fields; the requested markdown/text/
    # blocks channels are absent, so each must be named in a warning (never silently dropped).
    adapter = AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="extract"))
    req = _req(extraction_schema={"json_schema": {"type": "object", "properties": {}}})
    ctx = RunContext()
    resp = adapter.normalize(adapter.submit(req, ctx), ctx, req)
    assert resp.document.markdown is None and resp.document.text is None
    codes = {w.field for w in (resp.warnings or [])}
    assert {"markdown", "text", "blocks"} <= codes


def test_typed_field_type_set_from_extraction_schema():
    adapter = AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="extract"))
    req = _req(
        extraction_schema={
            "json_schema": {
                "type": "object",
                "properties": {
                    "gross_pay": {"type": "string"},
                    "pay_period_end": {"type": "string"},
                },
            }
        }
    )
    ctx = RunContext()
    resp = adapter.normalize(adapter.submit(req, ctx), ctx, req)
    # P2: TypedField.type comes from the extraction schema's declared type where available
    assert resp.typed_fields["gross_pay"].type == "string"
    assert resp.typed_fields["pay_period_end"].type == "string"
    # a field the schema doesn't declare gets no fabricated type
    assert resp.typed_fields["borrower_name"].type is None


def test_typed_field_citations_from_page_location():
    adapter = AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="extract_cited"))
    req = _req(
        extraction_schema={
            "json_schema": {"type": "object", "properties": {"gross_pay": {"type": "string"}}}
        }
    )
    ctx = RunContext()
    resp = adapter.normalize(adapter.submit(req, ctx), ctx, req)
    # P2: Claude page_location citations preserved on the typed fields (never dropped)
    cits = resp.typed_fields["borrower_name"].citations
    assert cits and cits[0].page == 2 and cits[0].text == "Jane Doe"


def test_page_count_prefers_pdf_page_count_when_bytes_parse():
    # a real single-page PDF's bytes drive an exact count via derive.pdf_page_count
    from openreading.derive import pdf_page_count

    try:
        import pymupdf  # noqa: F401
    except ImportError:
        pytest.skip("pymupdf not installed; pdf_page_count returns None → heuristic path")
    doc_bytes = _one_page_pdf_bytes()
    if pdf_page_count(doc_bytes) is None:
        pytest.skip("generated PDF not parseable in this environment")
    b64 = base64.b64encode(doc_bytes).decode()
    adapter = AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="extract"))
    req = _req(
        document={"bytes_base64": b64, "mime_type": "application/pdf"},
        extraction_schema={"json_schema": {"type": "object", "properties": {}}},
    )
    ctx = RunContext()
    # normalize() receives the slimmed request (bytes nulled) exactly like every production call
    # site — this is the repro for Ledger T4b F1: submit() must compute the exact page count from
    # `req`'s real bytes BEFORE slimming and carry it on job.raw.payload for normalize() to read.
    resp = adapter.normalize(adapter.submit(req, ctx), ctx, slim_request(req))
    # extract mode has no citations to count from → the PDF-bytes path fills page_count (was None)
    assert resp.document.page_count == 1


def _one_page_pdf_bytes() -> bytes:
    """A minimal but structurally valid single-page PDF (no external deps to build)."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
        b"0000000058 00000 n \n0000000115 00000 n \n"
        b"trailer<< /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF\n"
    )


def test_pdf_bytes_malformed_base64_falls_back_gracefully_not_a_crash():
    # Ledger T4b fix (Phase C round 2, trent Finding 4, Low): round 1's F1 fix relocated the
    # _pdf_bytes() call from normalize() (where slim_req's bytes are always None, making the call
    # effectively dead) into submit() (where real bytes are genuinely present) — this makes
    # _pdf_bytes()'s own `except (ValueError, OSError): return None` branch live in production for
    # the first time. It was previously uncovered by any test (trent's round 2 coverage finding).
    # Malformed base64 (bad padding) drives base64.b64decode to raise binascii.Error, a ValueError
    # subclass — submit() must swallow it via _pdf_bytes and fall through cleanly, never raise.
    adapter = AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="parse"))
    req = _req(document={"bytes_base64": "not-valid-base64", "mime_type": "application/pdf"})
    ctx = RunContext()
    resp = adapter.normalize(adapter.submit(req, ctx), ctx, slim_request(req))
    # No crash, and no exact byte-derived count is available (bytes were unparseable) — parse.json's
    # fixture still cites page 1, so the citation heuristic fills page_count exactly as it did
    # before submit() ever tried to read real bytes.
    assert resp.document.page_count == 1

    extract_adapter = AnthropicClaudeAdapter(client=FakeClaudeClient(fixture="extract"))
    extract_req = _req(
        document={"bytes_base64": "not-valid-base64", "mime_type": "application/pdf"},
        extraction_schema={"json_schema": {"type": "object", "properties": {}}},
    )
    extract_ctx = RunContext()
    extract_resp = extract_adapter.normalize(
        extract_adapter.submit(extract_req, extract_ctx), extract_ctx, slim_request(extract_req)
    )
    # extract.json's fixture has no citations at all → the heuristic also returns None, exercising
    # the fully-graceful "no exact count, no heuristic count either" fallback: still no crash.
    assert extract_resp.document.page_count is None


def test_model_override_via_backend_version():
    adapter = AnthropicClaudeAdapter(client=FakeClaudeClient())
    adapter.submit(
        _req(backend={"id": "anthropic-claude", "version": "claude-sonnet-5"}), RunContext()
    )
    assert adapter._client.last_call["model"] == "claude-sonnet-5"


def test_cost_from_token_usage():
    adapter = AnthropicClaudeAdapter(client=FakeClaudeClient())
    job = adapter.submit(_req(), RunContext())
    cost = adapter.report_cost(job)
    # opus-4-8: 2400 in @ $5/M + 180 out @ $25/M
    assert cost.cost_usd == pytest.approx(2400 / 1e6 * 5.0 + 180 / 1e6 * 25.0)
    assert cost.billing_target == "caller_account"


def test_rate_limit_maps_to_retryable():
    adapter = AnthropicClaudeAdapter(client=FakeClaudeClient(raise_exc=RateLimitError("slow down")))
    with pytest.raises(RetryableError):
        adapter.submit(_req(), RunContext())


def test_bad_request_maps_to_terminal():
    adapter = AnthropicClaudeAdapter(
        client=FakeClaudeClient(raise_exc=BadRequestError("too many pages"))
    )
    with pytest.raises(TerminalError):
        adapter.submit(_req(), RunContext())


@pytest.mark.live
def test_live_parse():  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    skip_unless_creds("anthropic-claude")
    resp = run_live(
        "anthropic-claude", AnthropicClaudeAdapter(), sample_pdf_request("anthropic-claude")
    )
    assert resp.document.text or resp.document.markdown


@pytest.mark.live
def test_live_liveness_probe():  # pragma: no cover
    """The real GET /v1/models via the SDK. This is the ONE vendor-kind probe, so this test is also
    what proves the "never billed" claim in practice: a models list creates no message and
    generates no tokens, so running it repeatedly costs nothing."""
    from openreading.liveness import check_liveness
    from openreading.types.liveness import LivenessStatus
    from tests.live_helpers import skip_unless_creds

    skip_unless_creds("anthropic-claude")
    report = check_liveness(AnthropicClaudeAdapter())
    assert report.measured is True
    assert report.status is LivenessStatus.LIVE, report.detail
