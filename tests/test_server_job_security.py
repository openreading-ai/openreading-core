"""Caller ownership and backend scope on the HTTP async-job read and delete paths."""

from __future__ import annotations

import base64
import time

import pytest

pytest.importorskip("fastapi", reason="server extra not installed")
pytest.importorskip("fitz", reason="pymupdf not installed")

from fastapi.testclient import TestClient  # noqa: E402

from openreading.adapters.registry import make_adapter  # noqa: E402
from openreading.server import create_app  # noqa: E402
from openreading.server.app import JobRecord, _principal_id  # noqa: E402
from openreading.testing.sample_pdf import build_sample_pdf  # noqa: E402
from openreading.types.enums import JobState, WaitMode  # noqa: E402
from openreading.types.job import Job  # noqa: E402
from openreading.types.request import OpenReadingRequest  # noqa: E402


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _pdf_body() -> dict:
    return {
        "document": {
            "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            "mime_type": "application/pdf",
        },
        "backend": {"id": "pymupdf"},
    }


def test_other_key_cannot_read_job_response(monkeypatch):
    monkeypatch.setenv("OPENREADING_API_KEYS", "owner-key,other-key")
    client = TestClient(create_app())
    submitted = client.post("/v1/jobs", json=_pdf_body(), headers=_auth("owner-key"))
    assert submitted.status_code == 200
    job_id = submitted.json()["job_id"]

    hidden = client.get(f"/v1/jobs/{job_id}", headers=_auth("other-key"))
    assert hidden.status_code == 404
    assert hidden.json()["error"]["category"] == "unknown_job"
    assert client.get(f"/v1/jobs/{job_id}", headers=_auth("owner-key")).status_code == 200


def test_other_key_cannot_drive_pending_job(monkeypatch):
    import openreading.server.app as app_module

    monkeypatch.setenv("OPENREADING_API_KEYS", "owner-key,other-key")
    app = create_app()
    client = TestClient(app)
    job = Job(
        id="pending-job", backend_id="pymupdf", wait_mode=WaitMode.POLL, state=JobState.RUNNING
    )
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/unused"}, "backend": {"id": "pymupdf"}}
    )
    app.state.jobs[job.id] = JobRecord(
        job.id,
        "pymupdf",
        make_adapter("pymupdf"),
        job,
        req,
        int(time.time() * 1000),
        principal=_principal_id("owner-key"),
    )
    drives = []

    def _drive(*args):
        drives.append(args)
        return job

    monkeypatch.setattr(app_module, "_drive_job", _drive)
    hidden = client.get(f"/v1/jobs/{job.id}", headers=_auth("other-key"))

    assert hidden.status_code == 404
    assert hidden.json()["error"]["category"] == "unknown_job"
    assert drives == []


def test_other_key_cannot_delete_job(monkeypatch):
    monkeypatch.setenv("OPENREADING_API_KEYS", "owner-key,other-key")
    client = TestClient(create_app())
    submitted = client.post("/v1/jobs", json=_pdf_body(), headers=_auth("owner-key"))
    assert submitted.status_code == 200
    job_id = submitted.json()["job_id"]

    hidden = client.delete(f"/v1/jobs/{job_id}", headers=_auth("other-key"))
    assert hidden.status_code == 404
    assert hidden.json()["error"]["category"] == "unknown_job"
    assert client.get(f"/v1/jobs/{job_id}", headers=_auth("owner-key")).status_code == 200
    assert client.delete(f"/v1/jobs/{job_id}", headers=_auth("owner-key")).status_code == 204


def test_owner_scope_is_checked_before_poll_drive(monkeypatch):
    import openreading.server.app as app_module

    token = "scoped-key"
    monkeypatch.setenv("OPENREADING_API_KEYS", token)
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", f"{token}=tesseract")
    app = create_app()
    client = TestClient(app)
    job = Job(
        id="scoped-job", backend_id="pymupdf", wait_mode=WaitMode.POLL, state=JobState.RUNNING
    )
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/unused"}, "backend": {"id": "pymupdf"}}
    )
    app.state.jobs[job.id] = JobRecord(
        job.id,
        "pymupdf",
        make_adapter("pymupdf"),
        job,
        req,
        int(time.time() * 1000),
        principal=_principal_id(token),
    )
    drives = []

    def _drive(*args):
        drives.append(args)
        return job

    monkeypatch.setattr(app_module, "_drive_job", _drive)
    denied = client.get(f"/v1/jobs/{job.id}", headers=_auth(token))

    assert denied.status_code == 403
    assert denied.json()["error"]["category"] == "scope_denied"
    assert drives == []


@pytest.mark.parametrize("endpoint", ["/v1/parse", "/v1/jobs"])
@pytest.mark.parametrize("url", ["https://[not-an-ip]/file.pdf", "https://[/file.pdf"])
def test_malformed_native_url_is_a_structured_refusal(endpoint, url, respx_mock, monkeypatch):
    monkeypatch.delenv("OPENREADING_API_KEYS", raising=False)
    client = TestClient(create_app(), raise_server_exceptions=False)
    reply = client.post(endpoint, json={"document": {"url": url}, "backend": {"id": "docling"}})
    assert reply.status_code == 502
    assert reply.json()["error"]["backend_code"] == "unsupported_input"
    assert respx_mock.calls.call_count == 0
