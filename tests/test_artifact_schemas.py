"""Vendored artifact contracts validate examples and reject unknown wire fields."""

import json
from importlib.resources import files

import jsonschema
import pytest
from pydantic import TypeAdapter

from openreading.artifacts.models import (
    ArtifactManifest,
    ErrorEnvelope,
    ImportReceipt,
    Passage,
    ReadResult,
    SearchResult,
)


@pytest.mark.parametrize(
    "name,model",
    [
        ("local-document.v0.3.json", ArtifactManifest),
        ("passage.v0.3.json", Passage),
        (
            "agent-document-tool.v0.3.json",
            TypeAdapter(ImportReceipt | SearchResult | ReadResult | ErrorEnvelope),
        ),
    ],
)
def test_models_match_vendored_contract(name, model):
    actual = json.loads(files("openreading.schemas").joinpath(name).read_text())
    jsonschema.Draft202012Validator.check_schema(actual)
    generated = model.model_json_schema() if isinstance(model, type) else model.json_schema()
    assert {k: v for k, v in actual.items() if k not in {"$schema", "$id"}} == generated
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"unknown": "secret"}, actual)


def test_engine_identity_and_manifest_do_not_encode_a_profile_backend_or_page_cap():
    from openreading.artifacts.models import EngineIdentity, FileRecord, artifact_id

    engine = EngineIdentity(
        core_version="0.3.0",
        backend_id="docling_local",
        backend_version="2",
        extraction_settings={},
    )
    manifest = ArtifactManifest(
        artifact_id=artifact_id("a" * 64, engine),
        document_sha256="a" * 64,
        display_name="large.pdf",
        source_relative_path="large.pdf",
        input_grant_sha256="b" * 64,
        page_count=10000,
        passage_count=1,
        engine=engine,
        created_at="2026-09-10T00:00:00Z",
        files={
            name: FileRecord(length=1, sha256="c" * 64)
            for name in ("source.pdf", "response.json", "passages.jsonl")
        },
    )
    assert manifest.engine.backend_id == "docling_local"
    assert manifest.page_count == 10000
    assert artifact_id("a" * 64, engine) != artifact_id(
        "a" * 64, engine.model_copy(update={"backend_id": "another_backend"})
    )
