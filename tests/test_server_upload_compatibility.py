"""Uploads preserve routing, strategy, cache, and pending-job behavior across HTTP encodings."""

from __future__ import annotations

import base64
import json
import tempfile

import pytest
from fastapi.testclient import TestClient

from openreading import schemas
from openreading.server import create_app
from openreading.testing.sample_pdf import build_sample_pdf


def _post(client, encoding, metadata, data=None, endpoint="/v1/parse"):
    data = data if data is not None else build_sample_pdf()
    if encoding == "multipart":
        return client.post(
            endpoint,
            files={"file": ("sample.pdf", data, "application/pdf")},
            data={"request": json.dumps(metadata)},
        )
    return client.post(
        endpoint,
        json={
            **metadata,
            "document": {
                "filename": "sample.pdf",
                "mime_type": "application/pdf",
                "bytes_base64": base64.b64encode(data).decode(),
            },
        },
    )


@pytest.fixture
def configured(tmp_path, monkeypatch):
    for name in (
        "OPENREADING_API_KEYS",
        "OPENREADING_API_KEY_SCOPES",
        "OPENREADING_SERVER_PATH_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "openreading.yaml"
    monkeypatch.setenv("OPENREADING_CONFIG", str(path))
    return path


@pytest.mark.parametrize("mode", ["default_chain", "named_strategy", "default_strategy"])
def test_upload_default_and_strategy_selection_matches_json(configured, mode):
    config = "version: 1\npolicy:\n  backends: [pymupdf]\nstrategies:\n  cheap: [pymupdf]\n"
    if mode == "default_strategy":
        config += "defaults:\n  strategy: cheap\n"
    configured.write_text(config)
    metadata = {"backend": {"id": "strategy:cheap" if mode == "named_strategy" else None}}
    results = []
    for encoding in ("json", "multipart"):
        with TestClient(create_app()) as client:
            response = _post(client, encoding, metadata)
        assert response.status_code == 200, response.text
        result = response.json()
        schemas.validate_response(result)
        assert result["backend"]["id"] == "pymupdf"
        assert "OpenReading Test Document" in result["document"]["text"]
        if mode != "default_chain":
            assert result.get("orchestration")
        results.append(result)
    assert results[0]["document"] == results[1]["document"]


@pytest.mark.parametrize("first", ["json", "multipart"])
def test_upload_and_json_share_content_cache_and_idempotency(configured, monkeypatch, first):
    from openreading.adapters.pymupdf import PyMuPDFAdapter

    configured.write_text("version: 1\npolicy:\n  backends: [pymupdf]\n")
    submitted = []
    original = PyMuPDFAdapter.submit

    def submit(self, request, context):
        submitted.append((request.idempotency_key, context.idempotency_key))
        return original(self, request, context)

    monkeypatch.setattr(PyMuPDFAdapter, "submit", submit)
    metadata = {"backend": {"id": None}, "idempotency_key": "same-upload-operation"}
    second = "multipart" if first == "json" else "json"
    document = build_sample_pdf()
    with TestClient(create_app()) as client:
        initial = _post(client, first, metadata, document)
        replay = _post(client, second, metadata, document)
    assert initial.status_code == replay.status_code == 200
    assert submitted == [("same-upload-operation", "same-upload-operation")]
    assert initial.json()["document"] == replay.json()["document"]
    assert "idempotent_replay" not in {w["code"] for w in initial.json().get("warnings", [])}
    assert "idempotent_replay" in {w["code"] for w in replay.json().get("warnings", [])}


def test_pending_upload_polls_after_spool_cleanup(configured, monkeypatch):
    import starlette.formparsers

    import openreading.api as api
    from openreading.types.enums import JobState, WaitMode
    from openreading.types.runtime import RawResult
    from tests.fakes import ConfigurableBackend, make_backend

    configured.write_text("version: 1\npolicy:\n  backends: [pymupdf]\n")
    spools = []
    factory = tempfile.SpooledTemporaryFile
    uploaded = b"synthetic pending upload content"

    def tracked(*args, **kwargs):
        kwargs["max_size"] = 1
        spool = factory(*args, **kwargs)
        spools.append(spool)
        return spool

    class PendingAdapter(ConfigurableBackend):
        def submit(self, request, context):
            assert spools and all(spool.closed for spool in spools)
            assert request.document.path is None
            assert request.document.filename == "sample.pdf"
            content = base64.b64decode(request.document.bytes_base64)
            assert content == uploaded
            job = self.new_job(WaitMode.POLL, state=JobState.RUNNING)
            job.next_poll_at = 0.0
            job.raw = RawResult(payload=content.decode())
            return job

        def poll(self, job, context):
            assert all(spool.closed for spool in spools)
            job.state = JobState.SUCCEEDED
            return job

    adapter = PendingAdapter(make_backend("pymupdf", local=True).descriptor)
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", tracked)
    monkeypatch.setattr(api, "make_adapter", lambda backend: adapter)
    with TestClient(create_app()) as client:
        submitted = _post(client, "multipart", {"backend": {"id": "pymupdf"}}, uploaded, "/v1/jobs")
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["state"] == "running"
        polled = client.get("/v1/jobs/" + submitted.json()["job_id"])
    assert polled.status_code == 200, polled.text
    assert polled.json()["state"] == "succeeded"
    result = polled.json()["response"]
    schemas.validate_response(result)
    assert result["document"]["text"] == uploaded.decode()
    assert spools and all(spool.closed for spool in spools)
