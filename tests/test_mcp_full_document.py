"""MCP preserves provider-returned OCR text and exposes the complete retained response."""

import dataclasses
import json

import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from openreading.adapters.docling_local.projection import project_document
from openreading.mcp_server.tools import create_server
from openreading.types.request import Outputs
from tests.test_artifact_document import reassemble, retain


@pytest.mark.asyncio
async def test_ocr_from_provider_through_normalization_storage_and_mcp(tmp_path):
    code = "OCR verification: O0-I1-90112.00"
    raw = {
        "pages": {str(i): {"size": {"width": 612, "height": 792}} for i in range(1, 4)},
        "items": [
            {"text": text, "label": "text", "prov": [{"page_no": page, "charspan": [0, len(text)]}]}
            for page, text in [
                (1, "Native header"),
                (2, "Long OCR context " * 500 + code),
                (3, "Native end"),
            ]
        ],
        "page_origins": {"1": "native", "2": "ocr", "3": "native"},
        "omitted_furniture_items": 1,
    }
    response, origins = project_document(raw, Outputs())
    assert response.document.pages[1].blocks[0].text == raw["items"][1]["text"]
    expected = response.to_schema_dict()
    service, identifier, _ = retain(tmp_path, expected, {str(k): v for k, v in origins.items()})
    service.config = dataclasses.replace(
        service.config, limits=dataclasses.replace(service.config.limits, document_bytes=2048)
    )
    results = []
    cursor = None
    async with create_connected_server_and_client_session(create_server(service)) as session:
        tools = (await session.list_tools()).tools
        tool = next(t for t in tools if t.name == "openreading_get_document")
        assert tool.annotations.readOnlyHint and tool.annotations.idempotentHint
        assert not tool.annotations.openWorldHint
        while True:
            result = await session.call_tool(
                "openreading_get_document", {"artifact_id": identifier, "cursor": cursor}
            )
            assert not result.isError
            assert result.structuredContent is None
            assert len(result.content) == 1
            assert len(result.content[0].text.encode()) <= 2048
            wire = json.loads(result.content[0].text)
            results.append(wire)
            cursor = wire["next_cursor"]
            if cursor is None:
                break
            assert len(results) < 200
        content = reassemble(results)
        assert content["response"] == expected
        assert content["response"]["document"]["pages"][1]["blocks"][0]["text"].endswith(code)
        assert content["page_origins"]["2"] == "ocr"
        assert any(w["code"] == "furniture_text_omitted" for w in content["response"]["warnings"])
        refs = [r for r in content["evidence"] if r["page"] == 2]
        assert all(r["text_origin"] == "ocr" for r in refs)
        # The exact code is located by complete document access, without a search call.
        read = await session.call_tool(
            "openreading_read",
            {"artifact_id": identifier, "evidence_ids": [refs[-1]["evidence_id"]]},
        )
        assert code in json.loads(read.content[0].text)["passages"][0]["text"]
        with pytest.raises(McpError) as error:
            await session.call_tool(
                "openreading_get_document", {"artifact_id": identifier, "path": "/private-secret"}
            )
        assert "private-secret" not in str(error.value)
        failed = await session.call_tool(
            "openreading_get_document", {"artifact_id": identifier, "cursor": "private-secret"}
        )
        assert failed.isError
        assert json.loads(failed.content[0].text)["error"]["code"] == "invalid_cursor"
        assert "private-secret" not in failed.content[0].text
    service.close()
