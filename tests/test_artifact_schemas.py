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
        ("local-document.v1.0.json", ArtifactManifest),
        ("passage.v1.0.json", Passage),
        (
            "agent-document-tool.v1.0.json",
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
