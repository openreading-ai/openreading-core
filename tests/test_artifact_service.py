"""Local imports cannot escape grants, bypass caps, or return corrupted evidence."""

import dataclasses
import json
from pathlib import Path

import pymupdf
import pytest

from openreading.artifacts.limits import ArtifactError, ProfileConfig, ProfileLimits
from openreading.artifacts.service import ArtifactService


@pytest.fixture
def service(tmp_path):
    root = tmp_path.resolve() / "input"
    root.mkdir()
    return ArtifactService(ProfileConfig(root, tmp_path.resolve() / "store"))


def pdf(path: Path, texts=("Provide notice at least 60 days before renewal.",)):
    with pymupdf.open() as doc:
        for text in texts:
            page = doc.new_page()
            if text:
                page.insert_text((50, 50), text)
        doc.save(path)


def test_import_restart_exact_evidence_and_dedup(service):
    pdf(
        service.config.input_root / "contract.pdf",
        ("", "Provide notice at least 60 days before renewal."),
    )
    receipt = service.import_document("contract.pdf")
    assert receipt.page_count == 2
    assert receipt.passage_count == 1
    assert not receipt.reused
    assert "60 days" not in json.dumps(receipt.wire())
    restarted = ArtifactService(service.config)
    assert restarted.import_document("contract.pdf").reused
    result = restarted.search(receipt.artifact_id, "renewal")
    assert result.hits[0].page == 2
    read = restarted.read(receipt.artifact_id, [result.hits[0].evidence_id])
    assert "60 days" in read.passages[0].text
    assert read.passages[0].bbox.page == 2


@pytest.mark.parametrize(
    "relative", ["../secret.pdf", "/etc/passwd", "a/../b", "a//b", "", ".", "a\x00b"]
)
def test_paths_are_refused(service, relative):
    with pytest.raises(ArtifactError, match="access_denied"):
        service.import_document(relative)


def test_symlink_and_directory_are_refused(service):
    pdf(service.config.input_root / "real.pdf")
    (service.config.input_root / "link.pdf").symlink_to("real.pdf")
    for name in ["link.pdf", "."]:
        with pytest.raises(ArtifactError, match="access_denied"):
            service.import_document(name)


def test_limits_and_failure_cleanup(service):
    pdf(service.config.input_root / "two.pdf", ("first", "second"))
    limited = ArtifactService(
        dataclasses.replace(service.config, limits=dataclasses.replace(ProfileLimits(), pages=1))
    )
    with pytest.raises(ArtifactError, match="input_too_large"):
        limited.import_document("two.pdf")
    assert list((service.config.artifact_root / "staging").iterdir()) == []
    assert list((service.config.artifact_root / "documents").rglob("manifest.json")) == []


def test_corruption_is_refused_after_restart(service):
    pdf(service.config.input_root / "contract.pdf")
    receipt = service.import_document("contract.pdf")
    retained = next(service.config.artifact_root.rglob("source.pdf"))
    retained.write_bytes(b"changed")
    with pytest.raises(ArtifactError, match="artifact_corrupt"):
        ArtifactService(service.config).search(receipt.artifact_id, "notice")


def test_new_grant_cannot_read_old_artifact(service):
    pdf(service.config.input_root / "contract.pdf")
    receipt = service.import_document("contract.pdf")
    other = service.config.input_root.parent / "other"
    other.mkdir()
    switched = ArtifactService(ProfileConfig(other, service.config.artifact_root))
    with pytest.raises(ArtifactError, match="artifact_not_found"):
        switched.load_artifact(receipt.artifact_id)


def test_no_text_and_non_pdf_are_explicit_errors(service):
    pdf(service.config.input_root / "empty.pdf", ("",))
    (service.config.input_root / "fake.pdf").write_text("private text")
    for name, code in [
        ("empty.pdf", "no_readable_text"),
        ("fake.pdf", "unsupported_format"),
        ("missing.pdf", "input_not_found"),
    ]:
        with pytest.raises(ArtifactError, match=code):
            service.import_document(name)
