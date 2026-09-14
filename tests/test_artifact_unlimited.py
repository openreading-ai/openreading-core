"""Uncapped Docling imports preserve streamed inputs and explicit operator refusals."""

import hashlib
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from openreading.artifacts.intake import copy_source
from openreading.artifacts.limits import ArtifactError, DoclingLimits, ProfileConfig
from openreading.artifacts.service import ArtifactService
from openreading.artifacts.store import safe_read


def unlimited():
    return DoclingLimits(
        source_bytes=None,
        pages=None,
        extraction_bytes=None,
        store_bytes=None,
        deadline_seconds=None,
        worker_memory_bytes=None,
        worker_idle_seconds=60,
    )


def test_no_implicit_source_quota_and_streamed_fingerprint(tmp_path, monkeypatch):
    source = tmp_path / "large.pdf"
    block = b"0123456789abcdef" * 4096
    with source.open("wb") as stream:
        for _ in range(417):
            stream.write(block)
    target = tmp_path / "copy.pdf"
    with source.open("rb") as stream:
        digest, changed = copy_source(stream.fileno(), target, None, None)
    assert not changed
    assert target.stat().st_size > 25 * 1024**2

    def forbidden(*args, **kwargs):
        pytest.fail("Fingerprinting must not read the entire PDF into memory")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    from openreading.artifacts.store import file_record

    record = file_record(target, None)
    assert record.length == 417 * len(block)
    assert record.sha256 == digest == hashlib.sha256(block * 417).hexdigest()
    with pytest.raises(ValueError):
        file_record(target, 100)


def test_unlimited_deadline_still_honors_explicit_cancel():
    service = object.__new__(ArtifactService)
    service.config = SimpleNamespace(limits=unlimited())
    service._check_time(-1e12, None)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ArtifactError, match="cancelled"):
        service._check_time(-1e12, cancelled)
    service.config = SimpleNamespace(limits=replace(unlimited(), deadline_seconds=1))
    with pytest.raises(ArtifactError, match="timeout"):
        service._check_time(-1e12, None)


def test_optional_caps_do_not_accept_invalid_values():
    for field in (
        "source_bytes",
        "pages",
        "extraction_bytes",
        "store_bytes",
        "worker_memory_bytes",
    ):
        for value in (0, -1, True, 1.5, float("inf")):
            with pytest.raises(ValueError):
                replace(unlimited(), **{field: value})


def test_uncapped_reader_still_refuses_symlinks(tmp_path):
    (tmp_path / "data").write_bytes(b"test")
    assert safe_read(tmp_path / "data", None) == b"test"
    (tmp_path / "link").symlink_to("data")
    with pytest.raises(ArtifactError, match="configuration_required"):
        safe_read(tmp_path / "link", None)


def test_worker_accepts_251_pages_and_passes_a_path(tmp_path, monkeypatch):
    from openreading.adapters.docling_local.client import LocalDoclingClient
    from openreading.adapters.docling_local.config import LocalDoclingConfig
    from openreading.artifacts import worker
    from openreading.artifacts.models import EngineIdentity
    from openreading.artifacts.supervisor import WarmWorker

    source = tmp_path / "input"
    source.mkdir()
    (source / "test.pdf").write_bytes(b"%PDF-test")
    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})
    monkeypatch.setattr(
        "openreading.adapters.docling_local.client.preflight_pdf", lambda p: (251, False)
    )

    def convert_path(self, path):
        assert isinstance(path, Path)
        assert path.name == "source.pdf"
        return {
            "pages": {str(n): {"size": {"width": 100, "height": 100}} for n in range(1, 252)},
            "items": [
                {
                    "text": "Retained last page",
                    "label": "text",
                    "prov": [{"page_no": 251, "charspan": [0, 18]}],
                }
            ],
            "page_origins": {str(n): "native" for n in range(1, 252)},
        }

    monkeypatch.setattr(LocalDoclingClient, "convert_path", convert_path)
    monkeypatch.setattr(LocalDoclingClient, "convert", lambda *a: pytest.fail("PDF bytes copied"))

    def run(self, job, *, check, progress=None):
        from openreading.artifacts.models import json_bytes

        origins = worker.extract(job, progress=progress)
        Path(job["directory"], "result.json").write_bytes(
            json_bytes({"ok": True, "page_origins": origins})
        )

    monkeypatch.setattr(WarmWorker, "run", run)
    config = ProfileConfig(source, tmp_path / "store", unlimited(), LocalDoclingConfig(tmp_path))
    service = ArtifactService(
        config,
        identity=EngineIdentity(
            core_version="test",
            backend_id="docling_local",
            backend_version="test",
            extraction_settings={"assets": {}},
        ),
    )
    try:
        receipt = service.import_document("test.pdf")
        assert receipt.page_count == 251
        assert service.search(receipt.artifact_id, "last").hits[0].page == 251
        retained = next(service.store.documents.rglob("source.pdf"))
        with retained.open("r+b") as stream:
            stream.write(b"broken")
        with pytest.raises(ArtifactError, match="artifact_corrupt"):
            service.load_artifact(receipt.artifact_id)
    finally:
        service.close()
