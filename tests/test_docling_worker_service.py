"""Offline conversion fixtures exercise retained evidence through the real worker writer."""

import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from openreading.adapters.docling_local.config import LocalDoclingConfig
from openreading.artifacts import worker
from openreading.artifacts.limits import ArtifactError, DoclingLimits, ProfileConfig
from openreading.artifacts.models import EngineIdentity
from openreading.artifacts.service import ArtifactService


@pytest.fixture
def extraction(monkeypatch):
    from openreading.adapters.docling_local.client import LocalDoclingClient

    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {"model": "fixed"})
    monkeypatch.setattr(
        LocalDoclingClient,
        "convert",
        lambda self, data: {
            "pages": {"1": {"size": {"width": 100, "height": 100}}},
            "items": [
                {
                    "text": "Renewal needs 60 days notice.",
                    "label": "text",
                    "prov": [{"page_no": 1, "charspan": [0, 29]}],
                }
            ],
            "page_origins": {"1": "ocr"},
        },
    )
    from openreading.adapters.docling_local import client

    monkeypatch.setattr(client, "preflight_pdf", lambda path: (1, False))


def job(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "source.pdf").write_bytes(b"%PDF-test")
    return {
        "directory": str(tmp_path),
        "pages": 10,
        "available": 100000,
        "extraction_bytes": 100000,
        "docling": LocalDoclingConfig(tmp_path).wire(),
        "expected_assets": {"model": "fixed"},
    }


def test_private_worker_loop_writes_origins_and_rejects_configuration_changes(
    tmp_path, monkeypatch, extraction
):
    value = job(tmp_path)
    value["id"] = "first"
    changed = {**value, "id": "second", "docling": {**value["docling"], "threads": 2}}
    monkeypatch.setattr(
        sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO((json.dumps(value) + "\n" + json.dumps(changed) + "\n").encode())
        ),
    )
    messages = []
    monkeypatch.setattr(worker.os, "set_inheritable", lambda *args: None)
    monkeypatch.setattr(worker.os, "write", lambda fd, data: messages.append(json.loads(data)))
    assert worker.serve_worker(99) == 0
    assert messages[-1] == {"id": "second", "error": "parse_failed"}
    assert {"id": "first", "ok": True} in messages
    result = json.loads((tmp_path / "result.json").read_bytes())
    assert result["page_origins"] == {"1": "ocr"}
    passage = json.loads((tmp_path / "passages.jsonl").read_bytes())
    assert passage["text_origin"] == "ocr"


def test_warm_service_restart_and_post_conversion_cancellation(tmp_path, monkeypatch, extraction):
    from openreading.artifacts.supervisor import WarmWorker

    root = tmp_path / "input"
    root.mkdir()
    (root / "test.pdf").write_bytes(b"%PDF-test")
    config = ProfileConfig(
        root,
        tmp_path / "store",
        DoclingLimits(
            pages=10, deadline_seconds=60, worker_memory_bytes=2**31, worker_idle_seconds=60
        ),
        LocalDoclingConfig(tmp_path),
    )
    identity = EngineIdentity(
        core_version="test",
        backend_id="docling_local",
        backend_version="test",
        extraction_settings={"assets": {"model": "fixed"}},
    )
    cancelled = threading.Event()
    cancel_after = [False]
    closed = []

    def run(self, value, *, check, progress=None):
        check()
        origins = worker.extract(value, progress=progress)
        Path(value["directory"], "result.json").write_text(
            json.dumps({"ok": True, "page_origins": origins})
        )
        if cancel_after[0]:
            cancelled.set()

    monkeypatch.setattr(WarmWorker, "run", run)
    monkeypatch.setattr(WarmWorker, "close", lambda self: closed.append(True))
    service = ArtifactService(config, identity=identity)
    try:
        receipt = service.import_document("test.pdf")
        hit = service.search(receipt.artifact_id, "renewal").hits[0]
        assert hit.text_origin == "ocr"
        assert service.read(receipt.artifact_id, [hit.evidence_id]).passages[0].page == 1
        assert service.import_document("test.pdf").reused
        (root / "test.pdf").write_bytes(b"%PDF-other")
        cancel_after[0] = True
        with pytest.raises(ArtifactError, match="cancelled"):
            service.import_document("test.pdf", cancelled=cancelled)
        assert closed
        assert not list((config.artifact_root / "staging").iterdir())
    finally:
        service.close()


@pytest.mark.parametrize(
    "limit,code", [("available", "storage_limit"), ("extraction_bytes", "extraction_too_large")]
)
def test_docling_writer_enforces_each_byte_budget(tmp_path, extraction, limit, code):
    value = job(tmp_path)
    value[limit] = 1
    with pytest.raises(ArtifactError, match=code):
        worker.extract(value)


def test_manifest_refuses_missing_or_invented_page_origins(tmp_path, monkeypatch, extraction):
    from pydantic import ValidationError

    from openreading.artifacts.models import ArtifactManifest
    from openreading.artifacts.supervisor import WarmWorker

    root = tmp_path / "input"
    root.mkdir()
    (root / "test.pdf").write_bytes(b"%PDF-test")
    config = ProfileConfig(
        root,
        tmp_path / "store",
        DoclingLimits(
            pages=10, deadline_seconds=60, worker_memory_bytes=2**31, worker_idle_seconds=60
        ),
        LocalDoclingConfig(tmp_path),
    )
    identity = EngineIdentity(
        core_version="test",
        backend_id="docling_local",
        backend_version="test",
        extraction_settings={"assets": {"model": "fixed"}},
    )

    def run(self, value, **kwargs):
        origins = worker.extract(value)
        Path(value["directory"], "result.json").write_text(
            json.dumps({"ok": True, "page_origins": origins})
        )

    monkeypatch.setattr(WarmWorker, "run", run)
    service = ArtifactService(config, identity=identity)
    try:
        receipt = service.import_document("test.pdf")
        value = service.load_artifact(receipt.artifact_id).wire()
        for origins in [{"2": "native"}, {"01": "native"}]:
            with pytest.raises(ValidationError):
                ArtifactManifest.model_validate({**value, "page_origins": origins})
    finally:
        service.close()


def test_partial_page_set_cannot_shrink_physical_source_count(tmp_path, monkeypatch, extraction):
    from openreading.adapters.docling_local import client

    monkeypatch.setattr(client, "preflight_pdf", lambda path: (2, False))
    with pytest.raises(ArtifactError, match="parse_failed"):
        worker.extract(job(tmp_path))
