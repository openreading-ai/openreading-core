"""Mistral OCR adapter happy paths, all offline through an injected Protocol fake.

The fixture mirrors Mistral's documented OCR response fields: pages with zero-based ``index``,
Markdown, pixel dimensions, native blocks/bounds/confidence, HTML tables, document annotations,
and ``usage_info.pages_processed``. It remains documented-shape evidence until a keyed live run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openreading.adapters.mistral_ocr import MistralOCRAdapter
from openreading.ledger.header import slim_request
from openreading.testing import ConformanceCase, check_adapter_conformance
from openreading.types import BlockType, JobState
from openreading.types.enums import WaitMode
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "mistral-ocr"


def _fixture(name: str = "ocr") -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


class FakeMistralOCRClient:
    """Plain fake implementing the adapter's client Protocol and recording request bodies."""

    def __init__(self) -> None:
        self.last_body: dict | None = None

    def ocr(self, body: dict) -> dict:
        self.last_body = body
        raw = _fixture()
        if "document_annotation_format" not in body:
            raw.pop("document_annotation", None)
        return raw


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.com/invoice.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "mistral-ocr"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _run(adapter: MistralOCRAdapter, req: OpenReadingRequest):
    job = adapter.submit(req, RunContext())
    assert job.wait_mode is WaitMode.INLINE and job.state is JobState.SUCCEEDED
    return adapter.normalize(job, RunContext(), slim_request(req)), job


def test_mistral_ocr_conforms() -> None:
    check_adapter_conformance(
        MistralOCRAdapter(client=FakeMistralOCRClient()),
        [ConformanceCase(request=_req(), deterministic=True, label="ocr")],
        strict_checks={"C1", "C6"},
        adapter_factory=lambda: MistralOCRAdapter(client=FakeMistralOCRClient()),
    )


def test_submit_uses_inline_document_url_and_default_contract() -> None:
    client = FakeMistralOCRClient()
    adapter = MistralOCRAdapter(client=client)
    job = adapter.submit(_req(), RunContext())

    assert job.wait_mode is WaitMode.INLINE and job.state is JobState.SUCCEEDED
    assert client.last_body == {
        "model": "mistral-ocr-latest",
        "document": {
            "type": "document_url",
            "document_url": "https://example.com/invoice.pdf",
        },
        "include_blocks": True,
        "confidence_scores_granularity": "block",
        "table_format": "html",
    }


def test_runtime_model_and_page_ranges_are_forwarded() -> None:
    client = FakeMistralOCRClient()
    req = _req(pages={"ranges": [{"start": 2, "end": 3}]})
    MistralOCRAdapter(client=client).submit(req, RunContext(runtime={"model": "ocr-test"}))

    assert client.last_body is not None
    assert client.last_body["model"] == "ocr-test"
    assert client.last_body["pages"] == [1, 2]


def test_request_model_override_wins_over_environment_config() -> None:
    client = FakeMistralOCRClient()
    req = _req(backend={"id": "mistral-ocr", "version": "mistral-ocr-requested"})
    MistralOCRAdapter(client=client).submit(req, RunContext(runtime={"model": "mistral-ocr-env"}))
    assert client.last_body is not None
    assert client.last_body["model"] == "mistral-ocr-requested"


@pytest.mark.parametrize(
    ("document", "expected_type", "expected_key", "expected_prefix"),
    [
        (
            {"bytes_base64": "JVBERi0=", "mime_type": "application/pdf"},
            "document_url",
            "document_url",
            "data:application/pdf;base64,JVBERi0=",
        ),
        (
            {"url": "https://example.com/scan.png", "mime_type": "image/png"},
            "image_url",
            "image_url",
            "https://example.com/scan.png",
        ),
        (
            {"bytes_base64": "iVBORw0=", "mime_type": "image/png"},
            "image_url",
            "image_url",
            "data:image/png;base64,iVBORw0=",
        ),
    ],
)
def test_document_shapes(document, expected_type, expected_key, expected_prefix) -> None:
    client = FakeMistralOCRClient()
    MistralOCRAdapter(client=client).submit(_req(document=document), RunContext())
    assert client.last_body is not None
    sent = client.last_body["document"]
    assert sent["type"] == expected_type
    assert sent[expected_key] == expected_prefix


def test_schema_extraction_forwards_json_schema_and_keeps_native_values() -> None:
    client = FakeMistralOCRClient()
    schema = {
        "type": "object",
        "properties": {"invoice_number": {"type": "string"}, "total": {"type": "number"}},
    }
    req = _req(extraction_schema={"json_schema": schema, "instructions": "Extract totals."})
    resp, job = _run(MistralOCRAdapter(client=client), req)

    assert client.last_body is not None
    assert client.last_body["document_annotation_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "openreading_extraction",
            "strict": True,
            "schema": schema,
        },
    }
    assert client.last_body["document_annotation_prompt"] == "Extract totals."
    assert resp.typed_fields is not None
    assert resp.typed_fields["invoice_number"].value == "1001"
    assert resp.typed_fields["total"].value == 42.5
    assert job.raw is not None and job.raw.object_class == "extract"


def test_normalizes_native_pages_blocks_geometry_confidence_and_derived_tables() -> None:
    resp, _ = _run(MistralOCRAdapter(client=FakeMistralOCRClient()), _req())

    assert resp.document.page_count == 2
    assert [p.page_number for p in resp.document.pages or []] == [1, 2]
    assert resp.document.markdown and "Invoice 1001" in resp.document.markdown
    assert resp.document.text and "Bill to Jane Doe." in resp.document.text
    first = (resp.document.pages or [])[0]
    assert first.width == 1700 and first.height == 2200 and first.dpi == 200
    title = next(b for b in first.blocks or [] if b.type is BlockType.TITLE)
    assert title.bbox is not None
    assert title.bbox.x == pytest.approx(85 / 1700)
    assert title.bbox.y == pytest.approx(110 / 2200)
    assert title.confidence == pytest.approx(0.98)
    table = next(b for b in first.blocks or [] if b.type is BlockType.TABLE)
    assert table.table is not None
    assert table.table.rows == [["Item", "Amount"], ["Consulting", "$42.50"]]
    assert "Item\tAmount" in (table.text or "")
    assert resp.channel_provenance == {
        "markdown": "native",
        "text": "derived",
        "blocks": "native",
        "block_bbox": "native",
        "block_confidence": "native",
        "table_cells": "derived",
    }


def test_zero_based_page_index_maps_to_one_based_page_numbers() -> None:
    """Mistral's ``pages[].index`` starts at 0 (OCRPageObject reference), the same basis as the
    0-based ``pages`` request filter; ``page_number`` is 1-based, so index 3 is page 4 even when
    only page 4 was requested. Mapping 0->1 while leaving 1 alone would number two pages 1."""
    raw = _fixture()
    raw["pages"] = [raw["pages"][1]]
    raw["pages"][0]["index"] = 3

    class _FilteredClient:
        def ocr(self, body: dict) -> dict:
            return raw

    req = _req(pages={"ranges": [{"start": 4}]})
    resp, _ = _run(MistralOCRAdapter(client=_FilteredClient()), req)
    assert [p.page_number for p in resp.document.pages or []] == [4]


def test_extraction_schema_without_json_schema_is_a_plain_ocr_call() -> None:
    """No ``json_schema`` means no ``document_annotation_format`` goes on the wire, so the job
    must not be stamped ``extract``: that stamp selects the annotated $5/1000 rate in
    ``report_cost`` and the ``extract`` operation label for a call Mistral bills as plain OCR."""
    client = FakeMistralOCRClient()
    adapter = MistralOCRAdapter(client=client)
    resp, job = _run(adapter, _req(extraction_schema={"instructions": "summarise"}))

    assert client.last_body is not None
    assert "document_annotation_format" not in client.last_body
    assert "document_annotation_prompt" not in client.last_body
    assert job.raw is not None and job.raw.object_class == "parse"
    assert resp.backend.operation == "parse"
    assert adapter.report_cost(job).native_quantity == 2


def test_cost_is_estimated_from_pages_and_annotation_mode() -> None:
    adapter = MistralOCRAdapter(client=FakeMistralOCRClient())
    _, parse_job = _run(adapter, _req())
    _, extract_job = _run(adapter, _req(extraction_schema={"json_schema": {"type": "object"}}))

    parse_cost = adapter.report_cost(parse_job)
    extract_cost = adapter.report_cost(extract_job)
    # Both report the page count Mistral returned. Annotation used to be priced a tenth of a cent
    # higher per page than plain OCR; that rate is gone and the pages
    # are identical either way, because the same document was sent.
    assert parse_cost.native_quantity == 2
    assert parse_cost.native_unit == "page"
    assert extract_cost.native_quantity == parse_cost.native_quantity


def test_missing_credentials_names_required_environment_variable() -> None:
    from openreading.types.errors import MissingCredentialsError

    with pytest.raises(MissingCredentialsError) as exc:
        MistralOCRAdapter().submit(_req(), RunContext())
    assert exc.value.missing == ["MISTRAL_API_KEY"]


def test_package_re_exports_the_adapter_and_the_client_protocol() -> None:
    # The openreading.adapters runbook, §2 "Files to CREATE", requires every adapter package to
    # re-export both its Adapter and its client Protocol. An integrator who follows that documented
    # path must not have to reach into the private `.adapter` module for one backend out of fifteen.
    import openreading.adapters.mistral_ocr as pkg
    from openreading.adapters.mistral_ocr.adapter import MistralOCRClient

    assert pkg.__all__ == ["MistralOCRAdapter", "MistralOCRClient"]
    assert pkg.MistralOCRAdapter is MistralOCRAdapter
    assert pkg.MistralOCRClient is MistralOCRClient


@pytest.mark.live
def test_live_mistral_ocr() -> None:  # pragma: no cover
    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds

    if not os.environ.get("MISTRAL_API_KEY"):
        pytest.skip("set MISTRAL_API_KEY to run Mistral OCR live tests")
    skip_unless_creds("mistral-ocr")
    resp = run_live("mistral-ocr", MistralOCRAdapter(), sample_pdf_request("mistral-ocr"))
    assert resp.document.markdown and resp.document.pages


# --- normalization edges: what the adapter does with a response that is not the happy shape ----
#
# `mistral-ocr` was the outlier of the fifteen adapters at 30 missed statements and 32 partial
# branches, against 1-9 for every other one. None of the gap needed a key: it is the tolerance
# code that reads a vendor payload defensively, and the channel-absence warnings that fire when a
# requested channel comes back empty. A backend that omits a channel must SAY so (the channel
# contract), so the warning arms are the part it would be worst to leave unproven.


class _ScriptedClient:
    """Returns exactly the payload a test hands it, so one test shapes one response."""

    def __init__(self, payload) -> None:
        self.payload = payload
        self.last_body: dict | None = None

    def ocr(self, body: dict):
        self.last_body = body
        return self.payload


def _normalized(payload, **req_over):
    adapter = MistralOCRAdapter(client=_ScriptedClient(payload))
    req = _req(**req_over)
    job = adapter.submit(req, RunContext())
    return adapter.normalize(job, RunContext(), slim_request(req)), adapter, job


def _codes(resp) -> set[str]:
    return {w.code for w in resp.warnings or []}


def test_a_non_object_response_is_a_terminal_malformed_response() -> None:
    adapter = MistralOCRAdapter(client=_ScriptedClient(["not", "an", "object"]))

    with pytest.raises(TerminalError) as e:
        adapter.submit(_req(), RunContext())

    assert e.value.backend_code == "malformed_response"


def test_inline_bytes_with_no_media_type_are_refused_rather_than_called_a_pdf() -> None:
    """D-v2-9 reversed by the removal set: unnamed bytes are the case core knows least about,
    which made it the least defensible place to invent a type."""
    adapter = MistralOCRAdapter(client=_ScriptedClient({"pages": []}))
    req = OpenReadingRequest.model_validate(
        {"document": {"bytes_base64": "eA=="}, "backend": {"id": "mistral-ocr"}}
    )

    with pytest.raises(TerminalError) as e:
        adapter.submit(req, RunContext())

    assert e.value.backend_code == "unsupported_input"


def test_an_image_is_recognized_by_filename_when_no_media_type_is_given() -> None:
    """`_is_image` falls back to the extension, and a URL's query string is not part of it."""
    adapter = MistralOCRAdapter(client=_ScriptedClient({"pages": []}))
    req = OpenReadingRequest.model_validate(
        {
            "document": {"url": "https://example.com/scan.png?sig=abc123"},
            "backend": {"id": "mistral-ocr"},
        }
    )

    adapter.submit(req, RunContext())

    assert adapter._client.last_body["document"]["type"] == "image_url"


def test_max_pages_alone_selects_that_many_pages_from_the_front() -> None:
    adapter = MistralOCRAdapter(client=_ScriptedClient({"pages": []}))

    adapter.submit(_req(pages={"max_pages": 3}), RunContext())

    assert adapter._client.last_body["pages"] == [0, 1, 2]


def test_an_absurd_max_pages_is_refused_before_the_call() -> None:
    adapter = MistralOCRAdapter(client=_ScriptedClient({"pages": []}))

    with pytest.raises(TerminalError) as e:
        adapter.submit(_req(pages={"max_pages": 20_000}), RunContext())

    assert e.value.backend_code == "page_range_too_large"


def test_an_absurd_page_range_is_refused_before_the_call() -> None:
    adapter = MistralOCRAdapter(client=_ScriptedClient({"pages": []}))

    with pytest.raises(TerminalError) as e:
        adapter.submit(_req(pages={"ranges": [{"start": 1, "end": 20_000}]}), RunContext())

    assert e.value.backend_code == "page_range_too_large"


def test_max_pages_caps_an_explicit_range() -> None:
    adapter = MistralOCRAdapter(client=_ScriptedClient({"pages": []}))

    adapter.submit(_req(pages={"ranges": [{"start": 1, "end": 5}], "max_pages": 2}), RunContext())

    assert adapter._client.last_body["pages"] == [0, 1]


def test_channel_absence_is_warned_not_silent_when_the_response_is_empty() -> None:
    """The channel contract: a channel a backend cannot produce is omitted with a `warnings[]`
    entry, never fabricated. A page carrying nothing at all must therefore name every requested
    channel it could not fill."""
    resp, _, _ = _normalized({"pages": [{"index": 0, "markdown": ""}]})

    codes = _codes(resp)
    assert "markdown_unavailable" in codes
    assert "text_unavailable" in codes
    assert "blocks_unavailable" in codes


def test_table_cells_requested_but_absent_is_warned() -> None:
    resp, _, _ = _normalized(
        {"pages": [{"index": 0, "markdown": "# Hi"}]}, outputs={"tables": "cells"}
    )

    assert "table_cells_unavailable" in _codes(resp)


def test_a_block_without_bounds_or_confidence_warns_on_both_channels() -> None:
    """Two separate warnings, because a caller reading bboxes and a caller reading confidence have
    different problems and neither should have to infer theirs from the other."""
    resp, _, _ = _normalized(
        {"pages": [{"index": 0, "markdown": "x", "blocks": [{"type": "text", "content": "x"}]}]}
    )

    codes = _codes(resp)
    assert "block_bbox_unavailable" in codes
    assert "block_confidence_unavailable" in codes


def test_document_annotation_that_is_not_json_is_partial_not_a_crash() -> None:
    resp, _, _ = _normalized(
        {"pages": [{"index": 0, "markdown": "x"}], "document_annotation": "{not json"},
        extraction_schema={"json_schema": {"type": "object"}},
    )

    assert resp.status.state.value == "partial"
    assert "typed_fields_malformed" in _codes(resp)


def test_document_annotation_that_is_json_but_not_an_object_is_partial() -> None:
    resp, _, _ = _normalized(
        {"pages": [{"index": 0, "markdown": "x"}], "document_annotation": "[1, 2]"},
        extraction_schema={"json_schema": {"type": "object"}},
    )

    assert resp.status.state.value == "partial"
    assert "typed_fields_malformed" in _codes(resp)


def test_typed_fields_requested_but_absent_is_warned_without_going_partial() -> None:
    resp, _, _ = _normalized(
        {"pages": [{"index": 0, "markdown": "x"}]},
        extraction_schema={"json_schema": {"type": "object"}},
    )

    assert resp.status.state.value == "succeeded"
    assert "typed_fields_unavailable" in _codes(resp)


def test_a_page_index_that_is_not_a_page_position_falls_back_to_the_ordinal() -> None:
    """`index` is the vendor's own zero-based page position. Anything that is not a non-negative
    int is not evidence of one, so the positional fallback is used rather than a coerced guess."""
    resp, _, _ = _normalized(
        {"pages": [{"index": "second", "markdown": "a"}, {"index": -1, "markdown": "b"}]}
    )

    assert [p.page_number for p in resp.document.pages] == [1, 2]


def test_unusable_page_dimensions_yield_no_bbox_rather_than_a_guessed_one() -> None:
    """A zero width is not a page, so the block keeps no bounds and the absence is warned."""
    resp, _, _ = _normalized(
        {
            "pages": [
                {
                    "index": 0,
                    "markdown": "x",
                    "dimensions": {"width": 0, "height": None},
                    "blocks": [
                        {
                            "type": "text",
                            "content": "x",
                            "top_left_x": 1,
                            "top_left_y": 1,
                            "bottom_right_x": 2,
                            "bottom_right_y": 2,
                        }
                    ],
                }
            ]
        }
    )

    block = resp.document.pages[0].blocks[0]
    assert block.bbox is None
    assert "block_bbox_unavailable" in _codes(resp)


def test_a_legacy_average_confidence_alias_is_read_without_being_documented_evidence() -> None:
    resp, _, _ = _normalized(
        {
            "pages": [
                {
                    "index": 0,
                    "markdown": "x",
                    "blocks": [
                        {"type": "text", "content": "x", "confidence_scores": {"average": 0.5}}
                    ],
                }
            ]
        }
    )

    assert resp.document.pages[0].blocks[0].confidence == 0.5


def test_a_non_dict_block_is_skipped_rather_than_failing_the_page() -> None:
    resp, _, _ = _normalized(
        {
            "pages": [
                {"index": 0, "markdown": "x", "blocks": ["junk", {"type": "text", "content": "ok"}]}
            ]
        }
    )

    blocks = resp.document.pages[0].blocks
    assert [b.text for b in blocks] == ["ok"]


def test_tables_none_strips_the_parsed_table_off_every_block() -> None:
    resp, _, _ = _normalized(_fixture(), outputs={"tables": "none"})

    assert all(b.table is None for p in resp.document.pages for b in p.blocks or [])


def test_a_table_entry_with_no_content_is_skipped() -> None:
    resp, _, _ = _normalized(
        {"pages": [{"index": 0, "markdown": "x", "tables": ["junk", {}, {"html": ""}]}]}
    )

    assert not any(b.table for p in resp.document.pages for b in p.blocks or [])


def test_pages_processed_falls_back_to_the_pages_actually_returned() -> None:
    """`usage_info.pages_processed` is the vendor's own count. A negative or non-int one is not a
    count, so the page count this response actually carries is used instead."""
    resp, _, _ = _normalized(
        {
            "pages": [{"index": 0, "markdown": "a"}, {"index": 1, "markdown": "b"}],
            "usage_info": {"pages_processed": -4},
        }
    )

    assert resp.usage.pages_processed == 2


def test_report_cost_counts_returned_pages_when_usage_info_is_unusable() -> None:
    _, adapter, job = _normalized(
        {
            "pages": [{"index": 0, "markdown": "a"}, {"index": 1, "markdown": "b"}],
            "usage_info": "not a dict",
        }
    )

    assert adapter.report_cost(job).native_quantity == 2.0
