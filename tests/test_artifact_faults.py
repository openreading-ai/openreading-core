"""Fault injection exercises import cleanup and preserves explicit local-only boundaries."""

import dataclasses
import json
import os
import threading

import pymupdf
import pytest

from openreading.artifacts.intake import copy_source
from openreading.artifacts.limits import ArtifactError, ProfileConfig, ProfileLimits
from openreading.artifacts.models import Passage
from openreading.artifacts.service import ArtifactService
from openreading.artifacts.worker import extract, main
from tests.test_artifact_service import pdf
from tests.test_artifact_service import service as service_fixture

service = service_fixture


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"source_bytes": 10}, "input_too_large"),
        ({"extraction_bytes": 10}, "extraction_too_large"),
        ({"store_bytes": 100}, "storage_limit"),
        ({"deadline_seconds": 0}, "timeout"),
    ],
)
def test_caps_leave_no_committed_artifact(service, kwargs, code):
    pdf(service.config.input_root / "test.pdf")
    limited = ArtifactService(
        dataclasses.replace(service.config, limits=dataclasses.replace(ProfileLimits(), **kwargs))
    )
    with pytest.raises(ArtifactError, match=code):
        limited.import_document("test.pdf")
    assert not list(service.config.artifact_root.rglob("manifest.json"))
    assert not list((service.config.artifact_root / "staging").iterdir())


def test_cancel_and_lock_are_observable(service):
    pdf(service.config.input_root / "test.pdf")
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ArtifactError, match="cancelled"):
        service.import_document("test.pdf", cancelled=cancelled)
    with service.store.import_lock(), pytest.raises(ArtifactError, match="busy"):
        service.import_document("test.pdf")
    assert not list((service.config.artifact_root / "staging").iterdir())


def test_timeout_kills_running_worker_and_releases_lock(service):
    pdf(service.config.input_root / "test.pdf")
    limited = ArtifactService(
        dataclasses.replace(
            service.config, limits=dataclasses.replace(ProfileLimits(), deadline_seconds=0.001)
        )
    )
    with pytest.raises(ArtifactError, match="timeout"):
        limited.import_document("test.pdf")
    assert service.import_document("test.pdf").page_count == 1


def test_password_is_not_echoed_or_accepted(service):
    with pymupdf.open() as doc:
        doc.new_page().insert_text((50, 50), "private text")
        doc.save(
            service.config.input_root / "locked.pdf",
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            user_pw="secret-password",
            owner_pw="owner",
        )
    with pytest.raises(ArtifactError, match="password_required") as error:
        service.import_document("locked.pdf")
    assert "secret-password" not in json.dumps(error.value.envelope().wire())


def test_roots_and_store_symlinks_fail_closed(tmp_path):
    root = tmp_path.resolve() / "input"
    root.mkdir()
    for store in [root, root / "nested", root.parent]:
        with pytest.raises(ArtifactError, match="configuration_required"):
            ArtifactService(ProfileConfig(root, store))
    link = root.parent / "link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ArtifactError, match="configuration_required"):
        ArtifactService(ProfileConfig(link, root.parent / "store"))


def test_ambient_hosted_config_cannot_change_profile(service, monkeypatch):
    (service.config.input_root / "openreading.yaml").write_text(
        "version: 1\npolicy:\n  backends: [reducto]\n"
    )
    monkeypatch.chdir(service.config.input_root)
    monkeypatch.setenv("OPENREADING_CONFIG", str(service.config.input_root / "openreading.yaml"))
    monkeypatch.setenv("OPENREADING_LEDGER", str(service.config.input_root / "unwanted"))
    pdf(service.config.input_root / "test.pdf")
    receipt = service.import_document("test.pdf")
    assert service.load_artifact(receipt.artifact_id).engine.backend_id == "pymupdf"
    assert not (service.config.input_root / "unwanted").exists()


def test_worker_meter_and_sanitization(tmp_path):
    root = tmp_path.resolve()
    pdf(root / "source.pdf")
    job = {"directory": str(root), "pages": 100, "extraction_bytes": 1000000, "available": 1}
    with pytest.raises(ArtifactError, match="storage_limit"):
        extract(job)
    (root / "job.json").write_text(json.dumps(job))
    assert main(["--job-file", str(root / "job.json")]) == 0
    assert json.loads((root / "result.json").read_text()) == {"error": "parse_failed"}


def test_source_growth_is_capped_before_extra_write(tmp_path, monkeypatch):
    path = tmp_path / "source"
    path.write_bytes(b"1234")
    fd = os.open(path, os.O_RDONLY)
    original_read = os.read
    first = True

    def growing_read(descriptor, count):
        nonlocal first
        if first:
            first = False
            with path.open("ab") as stream:
                stream.write(b"56789")
        return original_read(descriptor, count)

    monkeypatch.setattr(os, "read", growing_read)
    try:
        with pytest.raises(ArtifactError, match="input_too_large"):
            copy_source(fd, tmp_path / "copy", 5, 100)
    finally:
        os.close(fd)
    assert (tmp_path / "copy").stat().st_size == 0


def test_passage_invariants_cannot_invent_provenance():
    values = dict(
        evidence_id="p0001-b0000-s0000",
        page=1,
        block_index=0,
        segment_index=0,
        source_kind="block_text",
        text_start=0,
        text_end=3,
        text="abc",
    )
    for change in [
        {"text_end": 2},
        {"evidence_id": "p0002-b0000-s0000"},
        {"bbox": {"page": 2, "x": 0, "y": 0, "w": 0.5, "h": 0.5}},
        {"source_kind": "page_text", "bbox": {"page": 1, "x": 0, "y": 0, "w": 0.5, "h": 0.5}},
    ]:
        with pytest.raises(ValueError):
            Passage.model_validate(values | change)


def test_malformed_manifest_and_retained_symlink_are_refused(service):
    pdf(service.config.input_root / "test.pdf")
    receipt = service.import_document("test.pdf")
    manifest = next(service.config.artifact_root.rglob("manifest.json"))
    original = manifest.read_bytes()
    for raw in [[], {"format": "future"}, {"format": "local-document.v0.1"}]:
        manifest.write_text(json.dumps(raw))
        with pytest.raises(ArtifactError, match="artifact_corrupt|artifact_version_unsupported"):
            service.load_artifact(receipt.artifact_id)
    manifest.write_bytes(original)
    retained = manifest.parent / "source.pdf"
    retained.unlink()
    retained.symlink_to(service.config.input_root / "test.pdf")
    with pytest.raises(ArtifactError, match="artifact_corrupt"):
        service.load_artifact(receipt.artifact_id)


def test_cancelled_reimport_cannot_return_a_successful_cached_receipt(service):
    pdf(service.config.input_root / "test.pdf")
    service.import_document("test.pdf")
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ArtifactError, match="cancelled"):
        service.import_document("test.pdf", cancelled=cancelled)


def test_retained_read_uses_the_validated_parent_descriptor(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from openreading.artifacts import store

    root = tmp_path.resolve()
    parent = root / "retained"
    parent.mkdir()
    (parent / "text").write_bytes(b"expected")
    moved = root / "held"
    original = store.directory

    @contextmanager
    def replaced(path):
        with original(path) as fd:
            parent.rename(moved)
            parent.mkdir()
            (parent / "text").write_bytes(b"replacement")
            yield fd

    monkeypatch.setattr(store, "directory", replaced)
    assert store.safe_read(parent / "text", 100) == b"expected"


def test_worker_exit_race_preserves_cancellation(service, monkeypatch, tmp_path):
    from types import SimpleNamespace

    from openreading.artifacts import service as module

    class ExitedChild:
        pid = 123
        waited = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def poll(self):
            return None

        def wait(self):
            self.waited = True

    child = ExitedChild()
    monkeypatch.setattr(
        module, "subprocess", SimpleNamespace(Popen=lambda *a, **kw: child, DEVNULL=-3)
    )

    def disappeared(*args):
        raise ProcessLookupError

    monkeypatch.setattr(module.os, "killpg", disappeared)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ArtifactError, match="cancelled"):
        service._worker(tmp_path, 0, cancelled)
    assert child.waited
