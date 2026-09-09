"""Exercise uploaded DOCX bytes through HTTP ingress and the real Docling adapter offline."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi", reason="server extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from openreading import schemas  # noqa: E402
from openreading.server import create_app  # noqa: E402

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PARAGRAPH = "OpenReading synthetic upload preserves this paragraph."
TABLE_ROWS = (("Region", "Revenue"), ("North", "4400"))


def synthetic_docx() -> bytes:
    """Build a public synthetic paragraph and table without an optional document library."""
    rows = "".join(
        "<w:tr>"
        + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row)
        + "</w:tr>"
        for row in TABLE_ROWS
    )
    parts = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>'
        ),
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{PARAGRAPH}</w:t></w:r></w:p>"
            '<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w="4320"/>'
            f'<w:gridCol w:w="4320"/></w:tblGrid>{rows}</w:tbl>'
            '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr>'
            "</w:body></w:document>"
        ),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in parts.items():
            archive.writestr(name, contents)
    return output.getvalue()


def assert_docx_content(body: dict) -> None:
    """Require actual paragraph and table cells without fabricating DOCX page geometry."""
    schemas.validate_response(body)
    assert body["status"]["state"] == "succeeded"
    assert body["backend"]["id"] == "docling"
    document = body["document"]
    assert PARAGRAPH in document["text"]
    blocks = [block for page in document["pages"] for block in page.get("blocks", [])]
    assert any(PARAGRAPH in (block.get("text") or "") for block in blocks)
    tables = [block["table"] for block in blocks if block["type"] == "table"]
    assert tables
    cells = {cell["text"] for table in tables for cell in table["cells"]}
    assert {cell for row in TABLE_ROWS for cell in row} <= cells
    assert all(block.get("bbox") is None for block in blocks)
    assert all(cell.get("bbox") is None for table in tables for cell in table["cells"])
    assert all(
        page.get("width") is None and page.get("height") is None for page in document["pages"]
    )
    assert "block_bbox" not in body.get("channel_provenance", {})


def _docling_response() -> dict:
    """Replay the scrubbed synthetic DOCX response captured from Docling Serve 1.32.0."""
    fixture = Path(__file__).parent / "fixtures" / "docling" / "live_docx_upload.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


def test_upload_docx_reaches_docling_http_with_original_bytes(respx_mock, monkeypatch):
    monkeypatch.setenv("DOCLING_SERVE_URL", "https://docling.test")
    monkeypatch.delenv("OPENREADING_SERVER_PATH_ROOT", raising=False)
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    monkeypatch.delenv("OPENREADING_API_KEYS", raising=False)
    monkeypatch.delenv("OPENREADING_API_KEY_SCOPES", raising=False)
    docx = synthetic_docx()
    route = respx_mock.post("https://docling.test/v1/convert/source").mock(
        return_value=httpx.Response(200, json=_docling_response())
    )
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/parse",
            files={"file": ("/client-only/quarterly résumé.docx", docx, DOCX_MIME)},
            data={"request": json.dumps({"backend": {"id": "docling"}})},
        )
    assert response.status_code == 200, response.text
    assert route.call_count == 1
    payload = json.loads(route.calls[0].request.content)
    assert payload["sources"] == [
        {
            "kind": "file",
            "filename": "quarterly résumé.docx",
            "base64_string": base64.b64encode(docx).decode("ascii"),
        }
    ]
    assert "text" in payload["options"]["to_formats"]
    assert_docx_content(response.json())
