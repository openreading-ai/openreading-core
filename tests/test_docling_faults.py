"""Docling adapter — fault-injection for the branches the happy-path fixture skips: input
variants (bytes accepted, neither url nor bytes rejected), the schema-extraction guardrail,
submit error mapping through the shared `_map_error` (taxonomy pass-through, transient container
HTTP stays retryable, anything else terminal), health without a container, and normalize
tolerating a degenerate DoclingDocument (broken refs, empty body, missing provenance, non-dict
confidence, malformed key-value graph). All offline via an injected fake client."""

from __future__ import annotations

import pytest

from openreading.adapters._http import error_for_status
from openreading.adapters.docling import DoclingAdapter
from openreading.types import BlockType, JobState
from openreading.types.errors import RetryableError, TerminalError, UnsupportedFeatureError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext


def _req(**over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.test/doc.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "docling", "type": "oss_library"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _doc(**over) -> dict:
    """A minimal docling-serve envelope around a DoclingDocument with one 612x792 page."""
    ddoc: dict = {"pages": {"1": {"size": {"width": 612.0, "height": 792.0}}}, "body": {}}
    ddoc.update(over)
    return {"document": {"json_content": ddoc}}


class _ScriptedClient:
    """convert() returns a fixed payload, or raises a scripted exception."""

    def __init__(self, payload: dict | None = None, raise_exc: Exception | None = None) -> None:
        self._payload = payload if payload is not None else _doc()
        self._raise = raise_exc
        self.last_document: dict | None = None

    def convert(self, document, options):
        if self._raise is not None:
            raise self._raise
        self.last_document = document
        return self._payload


def _run(adapter, req):
    ctx = RunContext()
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


# ---- input variants ----------------------------------------------------------------------------


def test_bytes_input_is_sent_as_a_base64_file_source():
    client = _ScriptedClient()
    job = DoclingAdapter(client=client).submit(
        _req(
            document={
                "bytes_base64": "ZmFrZQ==",
                "mime_type": "application/pdf",
                "filename": "loan.pdf",
            }
        ),
        RunContext(),
    )
    assert job.state is JobState.SUCCEEDED
    assert client.last_document == {
        "kind": "file",
        "base64_string": "ZmFrZQ==",
        "filename": "loan.pdf",
    }


def test_neither_url_nor_bytes_is_unsupported_input():
    adapter = DoclingAdapter(client=_ScriptedClient())
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(document={"path": "/local.pdf"}), RunContext())
    assert exc.value.backend_code == "unsupported_input"


def test_schema_extraction_is_a_surfaced_unsupported_feature():
    adapter = DoclingAdapter(client=_ScriptedClient())
    req = _req(extraction_schema={"json_schema": {"total": "number"}})
    with pytest.raises(UnsupportedFeatureError) as exc:
        adapter.submit(req, RunContext())
    assert exc.value.feature == "custom_schema_extraction"


# ---- submit error mapping (_map_error) ----------------------------------------------------------


def test_submit_reraises_taxonomy_errors_unchanged():
    boom = TerminalError("bad source", backend_code="unsupported_input")
    adapter = DoclingAdapter(client=_ScriptedClient(raise_exc=boom))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "unsupported_input"  # not remapped to the exception type


def test_transient_container_status_stays_retryable():
    # A docling-serve container under load answers 503; _http.error_for_status already classifies
    # that as RetryableError, and the shared _map_error must not downgrade it to terminal.
    boom = error_for_status(503, {"retry-after": "7"}, message="model warming up")
    adapter = DoclingAdapter(client=_ScriptedClient(raise_exc=boom))
    with pytest.raises(RetryableError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.retry_after == 7.0


def test_submit_maps_unexpected_error_to_terminal():
    adapter = DoclingAdapter(client=_ScriptedClient(raise_exc=ValueError("boom")))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "ValueError"  # _map_error uses the exception type name


def test_map_error_never_rewraps_a_taxonomy_error():
    # The single decision point must be safe wherever it is called from.
    boom = RetryableError("model warming up", backend_code="http_503", retry_after=7.0)
    assert DoclingAdapter()._map_error(boom) is boom


# ---- health ------------------------------------------------------------------------------------


def test_health_is_ready_but_names_the_container_requirement():
    health = DoclingAdapter().health()  # no injected client
    assert health.ready and "container" in (health.detail or "")


# ---- normalize tolerates a degenerate DoclingDocument -------------------------------------------


def test_empty_document_yields_no_pages_and_no_channels():
    resp = _run(DoclingAdapter(client=_ScriptedClient()), _req())
    assert resp.document.pages == []
    assert resp.document.text is None and resp.document.markdown is None


def test_broken_body_refs_are_skipped():
    payload = _doc(
        texts=[{"label": "text", "text": "kept"}],
        body={
            "children": [
                {"$ref": "#/texts/0"},
                {"$ref": "#/texts/0"},  # already seen
                {"cref": None},  # no ref at all
                {"$ref": "#/nope"},  # not a two-part pointer
                {"$ref": "#/widgets/0"},  # collection absent from the document
                {"$ref": "#/texts/9"},  # index out of range
                {"$ref": "#/texts/x"},  # non-integer index
            ]
        },
    )
    resp = _run(DoclingAdapter(client=_ScriptedClient(payload)), _req())
    blocks = resp.document.pages[0].blocks
    assert [b.text for b in blocks] == ["kept"]


def test_empty_body_falls_back_to_array_order():
    payload = _doc(
        body={"children": []},
        texts=[{"label": "title", "text": "Title"}],
        pictures=[{"label": "picture"}],
    )
    resp = _run(DoclingAdapter(client=_ScriptedClient(payload)), _req())
    types = [b.type for b in resp.document.pages[0].blocks]
    assert types == [BlockType.TITLE, BlockType.IMAGE]


def test_unresolvable_caption_refs_are_skipped():
    payload = _doc(
        body={"children": [{"$ref": "#/pictures/0"}]},
        pictures=[{"label": "picture", "captions": [{"$ref": None}, {"$ref": "#/texts/7"}]}],
        texts=[{"label": "caption", "text": "orphan"}],
    )
    resp = _run(DoclingAdapter(client=_ScriptedClient(payload)), _req())
    types = [b.type for b in resp.document.pages[0].blocks]
    assert types == [BlockType.IMAGE]  # the picture stands alone; no caption fabricated


def test_item_without_provenance_gets_no_bbox():
    payload = _doc(
        body={"children": [{"$ref": "#/texts/0"}]}, texts=[{"label": "text", "text": "no prov"}]
    )
    resp = _run(DoclingAdapter(client=_ScriptedClient(payload)), _req())
    block = resp.document.pages[0].blocks[0]
    assert block.bbox is None  # never guessed from a missing ProvenanceItem


def test_non_reading_order_collection_ref_is_not_a_block():
    payload = _doc(
        body={"children": [{"$ref": "#/key_value_items/0"}, {"$ref": "#/texts/0"}]},
        texts=[{"label": "text", "text": "kept"}],
        key_value_items=[{"label": "key_value_region"}],
    )
    resp = _run(DoclingAdapter(client=_ScriptedClient(payload)), _req())
    assert [b.text for b in resp.document.pages[0].blocks] == ["kept"]


def test_unusable_confidence_report_is_not_fabricated():
    payload = _doc(body={"children": [{"$ref": "#/texts/0"}]}, texts=[{"label": "text"}])
    payload["confidence"] = "GOOD"  # a grade string, not the ConfidenceScores object
    resp = _run(DoclingAdapter(client=_ScriptedClient(payload)), _req())
    assert resp.document.confidence is None
    assert resp.document.pages[0].confidence is None


def test_out_of_range_confidence_scores_are_discarded():
    payload = _doc(body={"children": []})
    payload["confidence"] = {"ocr_score": 1.4, "parse_score": None, "layout_score": float("nan")}
    resp = _run(DoclingAdapter(client=_ScriptedClient(payload)), _req())
    assert resp.document.confidence is None


def test_typed_fields_tolerates_a_malformed_key_value_graph():
    graph = {
        "cells": [
            {"cell_id": 0, "label": "key", "text": "Dangling"},
            {"cell_id": 1, "label": "key", "text": "   "},
            {"cell_id": 2, "label": "value", "text": "orphan value"},
            {"cell_id": 3, "label": "key", "text": "Listed"},
            {
                "cell_id": 4,
                "label": "value",
                "text": "prov as a list",
                "prov": [{"page_no": 1, "bbox": {"l": 1.0, "t": 20.0, "r": 9.0, "b": 10.0}}],
            },
            {"cell_id": 5, "label": "key", "text": "Unlocated"},
            {"cell_id": 6, "label": "value", "text": "no prov at all"},
        ],
        "links": [
            {"source_cell_id": 0, "target_cell_id": 99},  # target cell does not exist
            {"source_cell_id": 1, "target_cell_id": 2},  # key cell has a blank name
            {"source_cell_id": 3, "target_cell_id": 4},
            {"source_cell_id": 5, "target_cell_id": 6},
        ],
    }
    payload = _doc(body={"children": []}, key_value_items=[{"graph": graph}])
    resp = _run(
        DoclingAdapter(client=_ScriptedClient(payload)), _req(outputs={"typed_fields": True})
    )
    assert set(resp.typed_fields) == {"Listed", "Unlocated"}
    assert resp.typed_fields["Listed"].citations[0].bbox is not None
    assert resp.typed_fields["Unlocated"].citations is None  # no geometry invented
