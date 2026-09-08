"""The 4th error-taxonomy member. A pure structural parser (pymupdf/tesseract/docling) that is
directly asked for schema-driven extraction must RAISE UnsupportedFeatureError rather than
silently drop the caller's primary ask (guardrail 1). The router pre-filters these at stage 2, so
the raise only fires on the direct-named path; an extraction-capable backend does not raise.
"""

from __future__ import annotations

import pytest

from openreading.adapters.registry import make_adapter
from openreading.types.errors import UnsupportedFeatureError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext
from tests.fakes import make_backend


def _req(backend_id: str, *, extract: bool) -> OpenReadingRequest:
    body = {
        "document": {"path": "/doc.pdf", "mime_type": "application/pdf"},
        "backend": {"id": backend_id},
    }
    if extract:
        body["extraction_schema"] = {"instructions": "extract totals and dates"}
    return OpenReadingRequest.model_validate(body)


@pytest.mark.parametrize("slug", ["pymupdf", "tesseract", "docling"])
def test_structural_parser_raises_unsupported_feature_for_extraction(slug):
    adapter = make_adapter(slug)
    with pytest.raises(UnsupportedFeatureError) as exc:
        adapter.submit(_req(slug, extract=True), RunContext())
    assert exc.value.feature == "custom_schema_extraction"


def test_extraction_capable_backend_does_not_raise_on_preflight():
    # a fake backend that advertises custom_schema_extraction passes assert_supports
    adapter = make_backend("extractor", custom_schema=True)
    adapter.assert_supports(_req("extractor", extract=True))  # no raise
