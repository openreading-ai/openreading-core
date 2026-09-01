"""Content-addressed idempotency cache: keys are stable on content, sensitive to
result-affecting options, and blind to secrets/transport."""

from __future__ import annotations

import sys
import threading

from openreading.router.cache import InMemoryResultCache, canonical_options, content_key
from openreading.router.executor import BoundedResultCache
from openreading.types import JobState, NormalizedResponse, WaitMode
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


# --- BoundedResultCache concurrency (M11) -----------------------------------------------


def test_bounded_result_cache_survives_concurrent_get_put():
    """M11: `execute_plan`'s idempotency cache is one `BoundedResultCache` shared across
    `run_in_threadpool` workers on the server's concurrent request path. `get`'s expiry branch is
    a compound check-then-delete over the backing `OrderedDict` (`now_ms >= expires` then `del`);
    two threads racing a `get` on the same expired key can both pass the check before either runs
    the `del`, and the second `del` raises `KeyError` — an idempotency-cache lookup should never
    be able to 500 a request. `ttl_ms=0` forces EVERY `get` down that expiry-delete branch (an
    entry is never fresh); all 8 threads cycle the SAME 4 keys in the SAME order so they are
    frequently hammering one key at once, and `sys.setswitchinterval` is dropped to 1us for the
    duration so CPython actually hands the GIL between threads inside the compound operations
    instead of letting one thread run an iteration to completion uninterrupted — without it the
    critical section is too short for the default 5ms switch interval to ever land inside it.

    This is a probabilistic hammer, not a deterministic repro: thread scheduling is not guaranteed
    to interleave badly within any fixed iteration count, so an unlucky run could pass even with
    the bug present (an earlier version of this test using default scheduling and offset per-worker
    key cycles passed 5/5 runs even with the lock removed — the race exists but needs to be forced
    into the open). The DETERMINISTIC guarantee that no exception can ever propagate is
    `self._lock` around the whole body of `get`/`put`, not this test's pass on any single run —
    break-fix evidence (5 runs with the lock removed, reported in task-7-report.md) surfaced a
    KeyError."""
    cache = BoundedResultCache(ttl_ms=0)
    keys = [f"k{i}" for i in range(4)]
    resp = NormalizedResponse.model_validate(
        {
            "status": {"state": "succeeded"},
            "backend": {"id": "x", "type": "oss_library"},
            "document": {"text": "t"},
        }
    )
    errors: list[BaseException] = []

    def hammer() -> None:
        try:
            for i in range(500):
                key = keys[i % len(keys)]
                cache.put(key, resp, now_ms=0)
                cache.get(key, now_ms=0)
        except BaseException as e:  # noqa: BLE001 - collected across threads, asserted on main
            errors.append(e)

    original_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(original_interval)

    assert errors == []
