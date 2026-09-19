"""Unpaginated normalized text remains readable without fabricated physical pages."""

import pytest

from openreading.artifacts.models import Passage
from openreading.artifacts.passages import iter_passages
from openreading.types.response import NormalizedResponse


def response(document):
    return NormalizedResponse.model_validate(
        {
            "status": {"state": "succeeded"},
            "backend": {"id": "test", "type": "oss_library"},
            "document": document,
        }
    )


def test_document_text_survives_without_a_page_or_origin():
    value = response({"text": "First paragraph.\nSecond paragraph: 東京."})
    passages = list(iter_passages(value))
    assert len(passages) == 1
    passage = passages[0]
    assert passage.text == value.document.text
    assert passage.evidence_id == "d0000-b0000-s0000"
    assert passage.source_pointer == "/document/text"
    assert passage.source_kind == "document_text"
    assert "page" not in passage.wire()
    assert "text_origin" not in passage.wire()
    assert "bbox" not in passage.wire()


def test_long_document_text_preserves_exact_offsets():
    text = "paragraph café 😀\n" * 700
    passages = list(iter_passages(response({"text": text})))
    assert len(passages) > 1
    assert "".join(p.text for p in passages) == text
    assert passages[0].text_start == 0 and passages[-1].text_end == len(text)
    for left, right in zip(passages, passages[1:], strict=False):
        assert left.text_end == right.text_start
    assert len({p.evidence_id for p in passages}) == len(passages)


def test_paginated_text_is_not_duplicated_by_document_fallback():
    value = response({"text": "whole", "pages": [{"page_number": 1, "text": "page"}]})
    passages = list(iter_passages(value))
    assert [p.text for p in passages] == ["page"]
    assert passages[0].page == 1
    assert passages[0].evidence_id == "p0001-b0000-s0000"


@pytest.mark.parametrize(
    "change",
    [
        {"page": 1},
        {"source_pointer": "/document/markdown"},
        {"source_kind": "page_text"},
        {"evidence_id": "p0001-b0000-s0000"},
    ],
)
def test_pageless_reference_cannot_claim_another_location(change):
    valid = {
        "evidence_id": "d0000-b0000-s0000",
        "block_index": 0,
        "segment_index": 0,
        "source_kind": "document_text",
        "source_pointer": "/document/text",
        "text_start": 0,
        "text_end": 5,
        "text": "hello",
    }
    assert Passage.model_validate(valid).text == "hello"
    with pytest.raises(ValueError):
        Passage.model_validate({**valid, **change})


def test_pageless_artifact_restarts_searches_and_delivers_without_invented_pages(tmp_path):
    import json

    from openreading.artifacts.service import ArtifactService
    from openreading.mcp_server.delivery import deliver_document
    from tests.test_artifact_document import retain

    value = response({"text": "A document has paragraph evidence."}).to_schema_dict()
    original, identifier, _ = retain(tmp_path, value)
    config = original.config
    original.close()
    service = ArtifactService(config)
    try:
        found = service.search(identifier, "paragraph")
        assert len(found.hits) == 1
        hit = found.hits[0]
        assert "page" not in hit.wire()
        assert hit.source_pointer == "/document/text"
        exact = service.read(identifier, [hit.evidence_id])
        assert exact.artifact_id == identifier
        assert exact.passages[0].text == value["document"]["text"]
        result = deliver_document(
            service, identifier, mode="auto", budget=1_000_000, root=None, request_id=1
        )
        payload = json.loads(result.content[0].text)
        assert payload["content"]["response"] == value
        assert payload["content"]["page_origins"] == {}
        assert payload["page_count"] is None
        assert payload["text_origins"] is None
        assert payload["empty_text_pages"] is None
        assert payload["content"]["evidence"][0]["source_pointer"] == "/document/text"
    finally:
        service.close()


def test_docling_preserves_unpaginated_provider_text_without_guessing_page_one():
    from openreading.adapters.docling_local.projection import project_document
    from openreading.types.request import Outputs

    value, origins = project_document(
        {"pages": {}, "items": [], "document_text": "A provider paragraph."}, Outputs()
    )
    assert value.document.text == "A provider paragraph."
    assert value.document.page_count is None
    assert value.document.pages is None
    assert origins == {}


def test_docling_worker_accepts_unpaginated_input_without_pdf_preflight(tmp_path, monkeypatch):
    import json

    from openreading.adapters.docling_local import client
    from openreading.adapters.docling_local.config import LocalDoclingConfig
    from openreading.artifacts.worker import extract

    (tmp_path / "source.md").write_text("# Source title\n\nBody.")
    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})

    def unexpected(path):
        raise AssertionError("Non-PDF input reached PDFium preflight")

    monkeypatch.setattr(client, "preflight_pdf", unexpected)

    class Client:
        def convert_path(self, path):
            assert path.name == "source.md"
            assert path.read_text() == "# Source title\n\nBody."
            return {"pages": {}, "items": [], "document_text": "Source title\nBody."}

    progress = []
    result = extract(
        {
            "directory": str(tmp_path),
            "source_file": "source.md",
            "pages": None,
            "available": None,
            "extraction_bytes": None,
            "docling": LocalDoclingConfig(tmp_path).wire(),
            "expected_assets": {},
        },
        client=Client(),
        page_progress=progress.append,
    )
    assert result == {}
    assert progress == []
    content = json.loads((tmp_path / "response.json").read_bytes())
    assert content["document"] == {"text": "Source title\nBody."}
    assert (
        json.loads((tmp_path / "passages.jsonl").read_bytes())["source_pointer"] == "/document/text"
    )


def test_import_keeps_source_extension_in_staging_and_identity(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from openreading.adapters.docling_local.client import LocalDoclingClient
    from openreading.adapters.docling_local.config import LocalDoclingConfig
    from openreading.artifacts.limits import DoclingLimits, ProfileConfig
    from openreading.artifacts.models import EngineIdentity
    from openreading.artifacts.service import ArtifactService
    from openreading.artifacts.supervisor import WarmWorker
    from openreading.artifacts.worker import extract

    root = tmp_path / "input"
    root.mkdir()
    for suffix in ("md", "txt"):
        (root / f"same.{suffix}").write_text("same bytes")
    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})

    def convert(self, path, **kwargs):
        assert path.name in {"source.md", "source.txt"}
        return {"pages": {}, "items": [], "document_text": path.read_text()}

    monkeypatch.setattr(LocalDoclingClient, "convert_path", convert)

    def run(self, job, **kwargs):
        origins = extract(job)
        Path(job["directory"], "result.json").write_text(json.dumps({"page_origins": origins}))

    monkeypatch.setattr(WarmWorker, "run", run)
    config = ProfileConfig(
        root,
        tmp_path / "store",
        DoclingLimits(
            pages=None, deadline_seconds=None, worker_memory_bytes=None, worker_idle_seconds=60
        ),
        LocalDoclingConfig(tmp_path),
    )
    identity = EngineIdentity(
        core_version="test",
        backend_id="docling_local",
        backend_version="test",
        extraction_settings={"assets": {}},
    )
    service = ArtifactService(config, identity=identity)
    try:
        first = service.import_document("same.md")
        second = service.import_document("same.txt")
        assert first.artifact_id != second.artifact_id
        assert first.page_count is None
        assert service.import_document("same.md").reused
        manifest = service.load_artifact(first.artifact_id)
        assert manifest.source_file == "source.md"
        assert "source.pdf" not in manifest.files
        assert (
            service.read(first.artifact_id, ["d0000-b0000-s0000"]).passages[0].text == "same bytes"
        )
    finally:
        service.close()


def test_legacy_pdf_artifact_remains_readable_without_rewriting(tmp_path):
    import json

    from openreading.artifacts.models import artifact_id, json_bytes
    from tests.test_artifact_document import retain, rich_response

    service, identifier, passages = retain(tmp_path, rich_response())
    try:
        directory = service.store.documents / identifier
        manifest = json.loads((directory / "manifest.json").read_bytes())
        engine = service.load_artifact(identifier).engine
        legacy_id = artifact_id(manifest["document_sha256"], engine, version="0.3")
        manifest.update(
            format="local-document.v0.3", evidence_format="passages.v0.3", artifact_id=legacy_id
        )
        manifest.pop("source_file")
        original = json_bytes(manifest)
        (directory / "manifest.json").write_bytes(original)
        legacy = directory.with_name(legacy_id)
        directory.rename(legacy)
        read = service.read(legacy_id, [passages[0].evidence_id])
        assert read.passages[0] == passages[0]
        assert (legacy / "manifest.json").read_bytes() == original
    finally:
        service.close()


@pytest.mark.parametrize("name", ["../source.md", "source../md", "source.", "source.md/extra", 4])
def test_worker_refuses_invalid_staged_source_names(tmp_path, name):
    from openreading.artifacts.limits import ArtifactError
    from openreading.artifacts.worker import extract

    with pytest.raises(ArtifactError, match="unsupported_format"):
        extract({"directory": str(tmp_path), "source_file": name})
