"""Delivery metadata discloses measured origins and empty pages without interpreting text."""

import hashlib
import json
from pathlib import Path

import pytest

from openreading.artifacts.models import json_bytes
from openreading.mcp_server.delivery import deliver_document
from tests.test_artifact_document import retain, rich_response
from tests.test_mcp_delivery import rpc_bytes


@pytest.mark.parametrize("mode", ["auto", "file"])
def test_delivery_discloses_origins_and_gaps_without_changing_export(tmp_path, mode):
    expected = rich_response("OCR text " * 300)
    expected["warnings"] = [{"code": "unreadable_pages", "message": "Empty page present"}]
    service, identifier, passages = retain(
        tmp_path, expected, {"1": "none", "2": "ocr", "3": "native"}
    )
    try:
        result = deliver_document(
            service, identifier, mode=mode, budget=1_000_000, root=None, request_id=1
        )
        receipt = json.loads(result.content[0].text)
        assert receipt["schema_version"] == "0.4"
        assert receipt["text_origins"] == {
            "native": 1,
            "ocr": 1,
            "mixed": 0,
            "unknown": 0,
            "none": 1,
            "unmeasured": 0,
        }
        assert receipt["empty_text_pages"] == {"total": 1, "pages": [1], "omitted": 0}
        assert receipt["parser_warnings"]["count_unit"] == "warning_records"
        assert receipt["parser_warnings"]["codes"] == [{"code": "unreadable_pages", "count": 1}]
        # Long source blocks produce multiple passages, without adding pages or OCR origins.
        assert receipt["passage_count"] == len(passages) > 3
        content = receipt.get("content") or json.loads(Path(receipt["local_path"]).read_bytes())
        assert content["response"] == {k: v for k, v in expected.items() if k != "backend_raw"}
        assert receipt["content_sha256"] == hashlib.sha256(json_bytes(content)).hexdigest()
    finally:
        service.close()


@pytest.mark.parametrize("origins", [{}, {"1": "none", "2": "unknown", "3": "mixed"}])
def test_origin_summary_keeps_absent_measurement_separate_from_recorded_unknown(tmp_path, origins):
    service, identifier, _ = retain(tmp_path, rich_response(), origins)
    try:
        result = deliver_document(
            service, identifier, mode="file", budget=4096, root=None, request_id=1
        )
        receipt = json.loads(result.content[0].text)
        assert receipt["text_origins"] == {
            "native": 0,
            "ocr": 0,
            "mixed": int(bool(origins)),
            "unknown": int(bool(origins)),
            "none": int(bool(origins)),
            "unmeasured": 0 if origins else 3,
        }
        assert receipt["empty_text_pages"]["pages"] == [1]
    finally:
        service.close()


def test_gap_preview_is_bounded_sorted_and_counts_pages_not_warning_records(tmp_path):
    expected = rich_response()
    expected["document"]["page_count"] = 21
    expected["document"]["pages"] = [
        {"page_number": n, "text": "" if n < 21 else "end", "blocks": []} for n in range(21, 0, -1)
    ]
    expected["warnings"] = [{"code": "unreadable_pages", "message": "Many empty pages"}] * 2
    service, identifier, _ = retain(
        tmp_path, expected, {str(n): "none" if n < 21 else "native" for n in range(1, 22)}
    )
    try:
        result = deliver_document(
            service, identifier, mode="file", budget=4096, root=None, request_id=1
        )
        receipt = json.loads(result.content[0].text)
        assert len(rpc_bytes(result)) <= 4096
        assert receipt["empty_text_pages"] == {
            "total": 20,
            "pages": list(range(1, 17)),
            "omitted": 4,
        }
        assert receipt["parser_warnings"]["total"] == 2
        assert receipt["parser_warnings"]["codes"] == [{"code": "unreadable_pages", "count": 2}]
        assert receipt["parser_warnings"]["count_unit"] == "warning_records"
    finally:
        service.close()


def test_empty_page_summary_keeps_block_only_text(tmp_path):
    expected = rich_response()
    expected["document"]["pages"][1]["text"] = ""
    service, identifier, _ = retain(tmp_path, expected)
    try:
        result = deliver_document(
            service, identifier, mode="auto", budget=1_000_000, root=None, request_id=1
        )
        receipt = json.loads(result.content[0].text)
        assert receipt["empty_text_pages"] == {"total": 1, "pages": [1], "omitted": 0}
        assert service.get_document(identifier).wire()["schema_version"] == "0.1"
    finally:
        service.close()
