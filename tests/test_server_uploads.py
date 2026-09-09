"""File uploads follow the existing HTTP request, authorization, and response contracts."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from openreading import schemas
from openreading.server import create_app
from openreading.testing.sample_pdf import build_sample_pdf


def upload_parts(metadata=None):
    return [
        ("file", ("sample.pdf", build_sample_pdf(), "application/pdf")),
        ("request", (None, json.dumps(metadata or {"backend": {"id": "pymupdf"}}))),
    ]


def test_upload_parse_matches_json_document(monkeypatch):
    monkeypatch.delenv("OPENREADING_SERVER_PATH_ROOT", raising=False)
    client = TestClient(create_app())
    uploaded = client.post("/v1/parse", files=upload_parts())
    assert uploaded.status_code == 200, uploaded.text
    schemas.validate_response(uploaded.json())
    encoded = client.post(
        "/v1/parse",
        json={
            "backend": {"id": "pymupdf"},
            "document": {
                "filename": "sample.pdf",
                "mime_type": "application/pdf",
                "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            },
        },
    )
    assert uploaded.json()["document"] == encoded.json()["document"]
    assert "OpenReading Test Document" in uploaded.json()["document"]["text"]


@pytest.mark.parametrize("endpoint", ["/v1/route", "/v1/jobs"])
def test_upload_single_document_endpoints(endpoint):
    client = TestClient(create_app())
    response = client.post(endpoint, files=upload_parts())
    assert response.status_code in (200, 202), response.text


def test_route_upload_does_not_execute(monkeypatch):
    import openreading.api as api

    def forbidden(*args, **kwargs):
        raise AssertionError("routing must not execute documents")

    monkeypatch.setattr(api, "run_request", forbidden)
    response = TestClient(create_app()).post("/v1/route", files=upload_parts())
    assert response.status_code == 200
    assert response.json()["chosen"] == "pymupdf"


def test_jobs_upload_result_survives_ingress_cleanup():
    client = TestClient(create_app())
    response = client.post("/v1/jobs", files=upload_parts())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "succeeded"
    polled = client.get("/v1/jobs/" + body["job_id"])
    assert polled.status_code == 200
    assert "OpenReading Test Document" in polled.json()["response"]["document"]["text"]


def test_jobs_upload_requires_named_backend():
    response = TestClient(create_app()).post(
        "/v1/jobs", files=upload_parts({"backend": {"id": None}})
    )
    assert response.status_code == 400
    assert "named backend" in response.json()["error"]["message"]


@pytest.mark.parametrize("endpoint", ["/v1/parse", "/v1/route", "/v1/jobs"])
def test_upload_limits_retain_413(endpoint, monkeypatch):
    import openreading.api as api

    monkeypatch.setattr(api, "_MAX_DOWNLOAD_BYTES", 8)
    response = TestClient(create_app()).post(endpoint, files=upload_parts())
    assert response.status_code == 413, response.text
    assert response.json()["error"]["backend_code"] == "doc_too_large"


def test_upload_auth_refuses_before_parsing(monkeypatch):
    import openreading.server.app as module

    monkeypatch.setenv("OPENREADING_API_KEYS", "upload-test-key")

    async def forbidden(request):
        raise AssertionError("unauthorized request was parsed")

    monkeypatch.setattr(module, "decode_request", forbidden)
    response = TestClient(create_app()).post("/v1/parse", files=upload_parts())
    assert response.status_code == 401


@pytest.mark.parametrize("endpoint", ["/v1/parse", "/v1/jobs"])
def test_upload_scope_refuses_before_dispatch(endpoint, monkeypatch):
    from openreading.adapters.pymupdf import PyMuPDFAdapter

    monkeypatch.setenv("OPENREADING_API_KEYS", "upload-test-key")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "upload-test-key=tesseract")

    def forbidden(*args, **kwargs):
        raise AssertionError("out-of-scope backend was dispatched")

    monkeypatch.setattr(PyMuPDFAdapter, "submit", forbidden)
    response = TestClient(create_app()).post(
        endpoint, files=upload_parts(), headers={"Authorization": "Bearer upload-test-key"}
    )
    assert response.status_code == 403, response.text


def test_batch_does_not_accept_multipart():
    response = TestClient(create_app()).post("/v1/batch", files=upload_parts())
    assert response.status_code == 400


@pytest.mark.parametrize("endpoint", ["/v1/route", "/v1/jobs"])
def test_candidate_retention_remains_parse_only(endpoint):
    metadata = {"backend": {"id": "pymupdf"}, "keep_candidates": True}
    response = TestClient(create_app()).post(endpoint, files=upload_parts(metadata))
    assert response.status_code == 400


def test_parse_retains_candidate_option(monkeypatch):
    import openreading.api as api

    captured = []
    original = api.run_request

    def run(req, **kwargs):
        captured.append(kwargs["keep_candidates"])
        return original(req, **kwargs)

    monkeypatch.setattr(api, "run_request", run)
    metadata = {"backend": {"id": "pymupdf"}, "keep_candidates": True}
    response = TestClient(create_app()).post("/v1/parse", files=upload_parts(metadata))
    assert response.status_code == 200
    assert captured == [True]


def test_upload_error_storage_is_sanitized(monkeypatch):
    import starlette.formparsers as parsing

    def unavailable(*args, **kwargs):
        raise OSError("private-storage-detail")

    monkeypatch.setattr(parsing, "SpooledTemporaryFile", unavailable)
    response = TestClient(create_app()).post("/v1/parse", files=upload_parts())
    assert response.status_code == 500
    assert "private-storage-detail" not in response.text


def test_openapi_exposes_both_encodings():
    spec = create_app().openapi()
    for endpoint in ("/v1/parse", "/v1/route", "/v1/jobs"):
        body = spec["paths"][endpoint]["post"]["requestBody"]
        assert set(body["content"]) == {"application/json", "multipart/form-data"}
        multipart = body["content"]["multipart/form-data"]["schema"]
        assert set(multipart["required"]) == {"file", "request"}
        assert multipart["properties"]["file"]["format"] == "binary"
        assert (
            "keep_candidates" in body["content"]["application/json"]["schema"]["properties"]
            if endpoint == "/v1/parse"
            else "keep_candidates"
            not in body["content"]["application/json"]["schema"]["properties"]
        )


@pytest.mark.parametrize("declared", [None, b"1"])
async def test_streamed_multipart_overflow_closes_partial_spool(declared, monkeypatch):
    import starlette.formparsers as parsing

    import openreading.server.app as module

    original = parsing.SpooledTemporaryFile
    spools = []

    def tracked(*args, **kwargs):
        spool = original(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(parsing, "SpooledTemporaryFile", tracked)
    monkeypatch.setattr(module, "_MAX_BODY_BYTES", 200)
    head = b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n\r\n'
    chunks = iter([head, b"x" * 201])
    messages = []

    async def receive():
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    async def send(message):
        messages.append(message)

    headers = [(b"content-type", b"multipart/form-data; boundary=x")]
    if declared:
        headers.append((b"content-length", declared))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/parse",
        "raw_path": b"/v1/parse",
        "query_string": b"",
        "headers": headers,
        "server": ("localhost", 80),
        "client": ("localhost", 123),
    }
    await create_app()(scope, receive, send)
    starts = [m for m in messages if m["type"] == "http.response.start"]
    assert len(starts) == 1 and starts[0]["status"] == 413
    assert spools and all(spool.closed for spool in spools)
    body = b"".join(m.get("body", b"") for m in messages)
    assert json.loads(body)["error"]["backend_code"] == "doc_too_large"
