"""Prove synthetic DOCX uploads against Docling Serve when DOCLING_SERVE_URL is configured.

OpenReading receives multipart through its ASGI HTTP client. Its real adapter then sends the
uploaded bytes over HTTP to the configured Docling Serve process, with no shared input path.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

pytest.importorskip("fastapi", reason="server extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from openreading.server import create_app  # noqa: E402
from tests.live_helpers import record_if_enabled, skip_unless_creds  # noqa: E402
from tests.test_server_docling_upload import (  # noqa: E402
    DOCX_MIME,
    assert_docx_content,
    synthetic_docx,
)


@pytest.mark.live
def test_live_docx_upload_through_openreading(monkeypatch, record_property):
    skip_unless_creds("docling")
    endpoint = os.environ["DOCLING_SERVE_URL"].rstrip("/")
    try:
        version = httpx.get(f"{endpoint}/version", timeout=10)
        version_text = version.text if version.is_success else f"HTTP {version.status_code}"
    except httpx.HTTPError as exc:
        version_text = f"Unavailable: {type(exc).__name__}"
    record_property("docling_serve_version", version_text)
    print(f"Docling Serve /version: {version_text}")
    monkeypatch.delenv("OPENREADING_SERVER_PATH_ROOT", raising=False)
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    monkeypatch.delenv("OPENREADING_API_KEYS", raising=False)
    monkeypatch.delenv("OPENREADING_API_KEY_SCOPES", raising=False)
    with TestClient(create_app()) as client:
        response = client.post(
            "/v1/parse",
            files={"file": ("synthetic-upload.docx", synthetic_docx(), DOCX_MIME)},
            data={
                "request": json.dumps(
                    {"backend": {"id": "docling"}, "outputs": {"include_backend_raw": True}}
                )
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    record_if_enabled("docling", "live_docx_upload", body["backend_raw"]["payload"])
    assert_docx_content(body)
