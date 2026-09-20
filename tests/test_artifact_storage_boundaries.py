"""Quota accounting ignores export links while evidence stays bound to its input grant."""

import dataclasses
import shutil

import pytest

from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.service import ArtifactService
from tests.test_artifact_service import pdf
from tests.test_artifact_service import service as service_fixture

service = service_fixture


@pytest.mark.parametrize("kind", ["file", "directory", "dangling", "export_root"])
def test_export_links_do_not_block_import_or_count_target_bytes(service, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    payload = outside / "large.json"
    payload.write_bytes(b"x" * 100_000)
    exports = service.config.artifact_root / "exports"
    before = service.store.size()
    if kind == "export_root":
        exports.symlink_to(outside, target_is_directory=True)
        expected = before
    else:
        exports.mkdir()
        (exports / "regular.json").write_bytes(b"retained export")
        target = {"file": payload, "directory": outside, "dangling": outside / "missing"}[kind]
        (exports / "link").symlink_to(target, target_is_directory=kind == "directory")
        expected = before + len(b"retained export")
    assert service.store.size() == expected
    pdf(service.config.input_root / "test.pdf")
    assert service.import_document("test.pdf").page_count == 1
    assert payload.read_bytes() == b"x" * 100_000


@pytest.mark.parametrize("area", ["documents", "staging", "worker", "exports-other"])
def test_managed_store_links_still_fail_closed(service, tmp_path, area):
    parent = service.config.artifact_root / area
    parent.mkdir(exist_ok=True)
    (parent / "link").symlink_to(tmp_path / "missing")
    with pytest.raises(ArtifactError, match="artifact_corrupt"):
        service.store.size()


def test_default_exports_consume_import_quota_and_manual_removal_reclaims_it(service):
    pdf(service.config.input_root / "test.pdf")
    before = service.store.size()
    exports = service.config.artifact_root / "exports"
    exports.mkdir()
    exported = exports / "saved.json"
    exported.write_bytes(b"x" * 100_000)
    assert service.store.size() == before + 100_000
    limited = ArtifactService(
        dataclasses.replace(
            service.config,
            limits=dataclasses.replace(service.config.limits, store_bytes=before + 100_000),
        )
    )
    try:
        with pytest.raises(ArtifactError, match="storage_limit"):
            limited.import_document("test.pdf")
        exported.unlink()
        assert limited.store.size() == before
        assert limited.import_document("test.pdf").page_count == 1
    finally:
        limited.close()


def test_copied_artifact_manifest_cannot_cross_input_grants(service, tmp_path):
    pdf(service.config.input_root / "test.pdf")
    receipt = service.import_document("test.pdf")
    assert service.load_artifact(receipt.artifact_id).input_grant_sha256 == service.store.grant
    other = tmp_path / "other-input"
    other.mkdir()
    switched = ArtifactService(ProfileConfig(other, service.config.artifact_root))
    try:
        source = service.store.documents / receipt.artifact_id
        copied = switched.store.documents / receipt.artifact_id
        shutil.copytree(source, copied)
        before = {path.name: path.read_bytes() for path in copied.iterdir()}
        assert before == {path.name: path.read_bytes() for path in source.iterdir()}
        assert switched.store.grant != service.store.grant
        with pytest.raises(ArtifactError, match="artifact_corrupt"):
            switched.load_artifact(receipt.artifact_id)
        assert before == {path.name: path.read_bytes() for path in copied.iterdir()}
    finally:
        switched.close()
