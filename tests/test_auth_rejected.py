"""auth_rejected mapping (GOAL2 6.3): a present-but-invalid key (provider 401/403, IAM
AccessDenied, GCP PermissionDenied, anthropic AuthenticationError) maps to a TerminalError with
backend_code='auth_rejected' — a distinct terminal signal (never a retry), which every surface
turns into a "check <env var>" hint. Offline: each adapter is driven with an injected fake client
that raises the provider's auth error; no network, no real key.

The second half of this file pins the shared hint machinery in `openreading.readiness`; the
per-surface wiring it feeds (CLI, batch, route --run, replay, calibrate, HTTP API) lives in
test_auth_rejected_surfaces.py."""

from __future__ import annotations

import pytest

from openreading.adapters._http import error_for_status
from openreading.adapters.registry import make_adapter
from openreading.readiness import (
    attach_auth_hint,
    auth_hinted,
    auth_rejected_backends,
    auth_rejected_hint,
    primary_secret_env,
)
from openreading.types.errors import RetryableError, TerminalError


def test_error_for_status_maps_401_403_to_auth_rejected():
    for status in (401, 403):
        e = error_for_status(status, {}, message="Unauthorized")
        assert isinstance(e, TerminalError)
        assert e.backend_code == "auth_rejected"
    # a 429 stays retryable (not auth)
    assert isinstance(error_for_status(429, {}), RetryableError)


def test_textract_maps_access_denied_to_auth_rejected():
    from openreading.adapters.aws_textract import AWSTextractAdapter

    class AccessDeniedException(Exception):
        pass

    adapter = AWSTextractAdapter()
    mapped = adapter._map_error(AccessDeniedException("not authorized to call Textract"))
    assert isinstance(mapped, TerminalError) and mapped.backend_code == "auth_rejected"


def test_textract_maps_clienterror_code_to_auth_rejected():
    from openreading.adapters.aws_textract import AWSTextractAdapter

    class ClientError(Exception):
        def __init__(self):
            self.response = {"Error": {"Code": "UnrecognizedClientException"}}

    adapter = AWSTextractAdapter()
    mapped = adapter._map_error(ClientError())
    assert mapped.backend_code == "auth_rejected"


def test_google_docai_maps_permission_denied_to_auth_rejected():
    from openreading.adapters.google_document_ai import GoogleDocumentAIAdapter

    class _Code:
        name = "PERMISSION_DENIED"

    class PermissionDenied(Exception):
        code = _Code()

    adapter = GoogleDocumentAIAdapter()
    mapped = adapter._map_error(
        PermissionDenied("caller lacks documentai.processors.processOnline")
    )
    assert isinstance(mapped, TerminalError) and mapped.backend_code == "auth_rejected"


def test_anthropic_maps_authentication_error_to_auth_rejected():
    from openreading.adapters.anthropic_claude import AnthropicClaudeAdapter

    class AuthenticationError(Exception):
        pass

    adapter = AnthropicClaudeAdapter()
    mapped = adapter._map_error(AuthenticationError("invalid x-api-key"))
    assert isinstance(mapped, TerminalError) and mapped.backend_code == "auth_rejected"


@pytest.mark.parametrize("status,is_auth", [(401, True), (403, True), (429, False), (500, False)])
def test_status_classification_matrix(status, is_auth):
    e = error_for_status(status, {})
    assert (e.backend_code == "auth_rejected") is is_auth


def test_413_maps_to_doc_too_large():
    e = error_for_status(413, {}, message="payload too large")
    assert isinstance(e, TerminalError) and e.backend_code == "doc_too_large"


def test_anthropic_maps_size_limit_to_doc_too_large():
    from openreading.adapters.anthropic_claude import AnthropicClaudeAdapter

    class RequestTooLargeError(Exception):
        pass

    adapter = AnthropicClaudeAdapter()
    mapped = adapter._map_error(RequestTooLargeError("document exceeds the maximum page limit"))
    assert mapped.backend_code == "doc_too_large"


# ---- the shared hint (readiness) --------------------------------------------------------------


def _auth_error(message: str = "HTTP 401") -> TerminalError:
    return TerminalError(message, backend_code="auth_rejected")


def test_primary_secret_env_prefers_the_required_secret():
    # reducto declares api_key (required secret) + webhook_secret; the key to check is the former
    assert primary_secret_env(make_adapter("reducto").descriptor) == "REDUCTO_API_KEY"


def test_primary_secret_env_falls_back_to_any_declared_var():
    # qwen-vl's api_key is OPTIONAL (a self-hosted endpoint may need none) — still the var to check
    assert primary_secret_env(make_adapter("qwen-vl").descriptor) == "QWEN_VL_API_KEY"


def test_primary_secret_env_is_none_for_a_local_backend():
    assert primary_secret_env(make_adapter("pymupdf").descriptor) is None


def test_hint_for_a_local_backend_names_no_var():
    msg = auth_rejected_hint("pymupdf")
    assert msg == "key was found but rejected by pymupdf"


def test_hint_for_an_unknown_slug_degrades_without_raising():
    assert auth_rejected_hint("not-a-backend") == "key was found but rejected by not-a-backend"


def test_attach_auth_hint_replaces_the_provider_body():
    # the provider's 401 body may echo the rejected key back — it must not survive
    exc = _auth_error('{"detail":"bad key sk_live_LEAK"}')
    attach_auth_hint(exc, make_adapter("reducto").descriptor)
    assert "sk_live_LEAK" not in str(exc)
    assert "REDUCTO_API_KEY" in str(exc) and str(exc) == exc.message


def test_attach_auth_hint_leaves_other_failures_alone():
    exc = TerminalError("document too large", backend_code="doc_too_large")
    attach_auth_hint(exc, make_adapter("reducto").descriptor)
    assert str(exc) == "document too large"


def test_attach_auth_hint_is_idempotent_so_the_innermost_boundary_wins():
    exc = _auth_error()
    attach_auth_hint(exc, make_adapter("reducto").descriptor)
    attach_auth_hint(exc, make_adapter("chunkr").descriptor)  # an outer boundary must not overwrite
    assert "REDUCTO_API_KEY" in str(exc) and "CHUNKR_API_KEY" not in str(exc)


def test_auth_hinted_reraises_and_stamps():
    with pytest.raises(TerminalError) as exc, auth_hinted(make_adapter("reducto").descriptor):
        raise _auth_error()
    assert "REDUCTO_API_KEY" in str(exc.value)


def test_auth_hinted_passes_a_non_adapter_error_through():
    with pytest.raises(ValueError, match="boom"), auth_hinted(make_adapter("reducto").descriptor):
        raise ValueError("boom")


def test_auth_rejected_backends_reads_both_trail_shapes():
    trail = [
        {"backend": "pymupdf", "category": "TerminalError", "code": "corrupt_document"},
        {"backend": "reducto", "category": "TerminalError", "code": "auth_rejected"},
        {"backend": "chunkr", "category": "error(auth)", "node": "root.steps[1]"},
        {"backend": "reducto", "category": "error(auth)", "node": "root.steps[2]"},
        {"category": "error(auth)"},  # a trail entry with no backend is skipped
    ]
    assert auth_rejected_backends(trail) == ["reducto", "chunkr"]
