"""Content-addressed idempotency cache: keys are stable on content, sensitive to
result-affecting options, and blind to secrets/transport."""

from __future__ import annotations

from openreading.router.cache import InMemoryResultCache, canonical_options, content_key
from openreading.types import JobState, WaitMode
from openreading.types.job import Job

DATA = b"%PDF-1.7 ... bytes ..."


def _req(**over):
    base = {
        "document": {"bytes_base64": "..."},
        "backend": {"id": "aws-textract", "operation": "AnalyzeDocument"},
        "outputs": {"blocks": True, "typed_fields": False},
    }
    base.update(over)
    return base


def test_key_is_stable_across_dict_ordering():
    a = content_key(DATA, "aws-textract", "v4", _req(outputs={"blocks": True, "text": False}))
    b = content_key(DATA, "aws-textract", "v4", _req(outputs={"text": False, "blocks": True}))
    assert a == b
    assert a.startswith("om_")


def test_key_changes_with_content_backend_version_and_options():
    base = content_key(DATA, "aws-textract", "v4", _req())
    assert base != content_key(DATA + b"x", "aws-textract", "v4", _req())  # bytes
    assert base != content_key(DATA, "google-document-ai", "v4", _req())  # backend
    assert base != content_key(DATA, "aws-textract", "v5", _req())  # version
    assert base != content_key(DATA, "aws-textract", "v4", _req(outputs={"typed_fields": True}))
    assert base != content_key(
        DATA,
        "aws-textract",
        "v4",
        _req(backend={"id": "aws-textract", "operation": "AnalyzeLending"}),
    )


def test_key_ignores_secrets_and_transport():
    with_secret = _req(
        backend={
            "id": "aws-textract",
            "operation": "AnalyzeDocument",
            "credentials_ref": "arn:secret",
        },
    )
    with_secret["async"] = {"mode": "async", "webhook_url": "https://a.test/hook"}
    with_secret["idempotency_key"] = "user-supplied"
    plain = _req(backend={"id": "aws-textract", "operation": "AnalyzeDocument"})
    assert content_key(DATA, "aws-textract", "v4", with_secret) == content_key(
        DATA, "aws-textract", "v4", plain
    )


def test_canonical_options_is_deterministic_json():
    s = canonical_options(_req())
    assert s == canonical_options(_req())
    assert '"operation":"AnalyzeDocument"' in s


def test_in_memory_cache_get_put():
    cache = InMemoryResultCache()
    key = content_key(DATA, "pymupdf", "1.28", _req(backend={"id": "pymupdf"}))
    assert cache.get(key) is None
    job = Job(
        id="omjob_1", backend_id="pymupdf", wait_mode=WaitMode.INLINE, state=JobState.SUCCEEDED
    )
    cache.put(key, job)
    got = cache.get(key)
    assert got is not None and got.id == "omjob_1"
