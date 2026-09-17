"""The shared execution boundary refuses named backends before resolving credentials."""

import pytest

from openreading.api import build_request, run_request
from openreading.credentials import EnvCredentialBroker
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ScopeRefused
from openreading.types.request import OpenReadingRequest


class NoCredentialAccess(EnvCredentialBroker):
    def resolve(self, descriptor, request):
        raise AssertionError("A denied backend reached credential resolution")


@pytest.mark.parametrize("backend", ["pymupdf", "unregistered-backend"])
@pytest.mark.parametrize("allowed", [frozenset(), frozenset({"docling_local"})])
def test_named_backend_is_denied_before_lookup_credentials_or_execution(backend, allowed):
    request = OpenReadingRequest.model_validate(
        {"document": {"bytes_base64": "bm90"}, "backend": {"id": backend}}
    )
    with pytest.raises(ScopeRefused) as failure:
        run_request(request, broker=NoCredentialAccess(), backend_allowlist=allowed)
    assert failure.value.backend_code == backend
    assert failure.value.constraint == "backend_allowlist"


@pytest.mark.parametrize("allowed", [None, frozenset({"pymupdf"})])
def test_allowed_or_unscoped_named_backend_still_executes(allowed):
    request = build_request(build_sample_pdf(), "pymupdf")
    result = run_request(request, backend_allowlist=allowed)
    assert result["backend"]["id"] == "pymupdf"
    assert result["document"]["pages"]
