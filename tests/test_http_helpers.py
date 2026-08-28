"""The shared HTTP helper (adapters/_http.py) — status→taxonomy mapping and Retry-After parsing.
Pure functions used by every httpx-based adapter (Azure DI, Reducto, Docling, Qwen-VL, Pulse,
Chunkr), so fault-injecting them here hardens all of those error paths at once. No network."""

from __future__ import annotations

import pytest

from openreading.adapters._http import (
    build_httpx_client,
    error_for_status,
    retry_after_seconds,
)
from openreading.types.errors import RetryableError, TerminalError

httpx = pytest.importorskip("httpx", reason="http extra not installed")


# ---- build_httpx_client -----------------------------------------------------------------------


def test_build_httpx_client_applies_base_url_and_headers():
    client = build_httpx_client(
        base_url="https://api.example.com", headers={"x-key": "v"}, timeout=12.0
    )
    try:
        assert isinstance(client, httpx.Client)
        assert str(client.base_url) == "https://api.example.com"
        assert client.headers["x-key"] == "v"
    finally:
        client.close()


@pytest.mark.parametrize("bad", ["nuextract.ai", "# a comment parsed as a value", "ftp://x"])
def test_build_httpx_client_rejects_schemeless_base_url_with_actionable_error(bad):
    # Defense-in-depth for the .env inline-comment bug: a non-empty base_url without http(s)://
    # must fail with an actionable TerminalError naming the value — not surface later as httpx's
    # cryptic "Request URL is missing an 'http://' or 'https://' protocol" on the first request.
    with pytest.raises(TerminalError, match="http:// or https://"):
        build_httpx_client(base_url=bad)


def test_build_httpx_client_empty_base_url_still_allowed():
    # some adapters build absolute-URL clients (base_url="") — that stays valid.
    client = build_httpx_client()
    try:
        assert isinstance(client, httpx.Client)
    finally:
        client.close()


# ---- error_for_status: status -> error taxonomy -----------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_statuses_are_terminal_auth_rejected(status):
    err = error_for_status(status)
    assert isinstance(err, TerminalError)
    assert err.backend_code == "auth_rejected"  # the CLI turns this into a "check <env var>" hint


def test_413_is_terminal_doc_too_large():
    err = error_for_status(413)
    assert isinstance(err, TerminalError)
    assert err.backend_code == "doc_too_large"  # router MAY fall back to a higher-ceiling backend


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_retryable_statuses_map_to_retryable_error(status):
    err = error_for_status(status)
    assert isinstance(err, RetryableError)
    assert err.backend_code == f"http_{status}"


def test_retryable_carries_retry_after_from_headers():
    err = error_for_status(429, {"retry-after": "7"})
    assert isinstance(err, RetryableError)
    assert err.retry_after == 7.0


@pytest.mark.parametrize("status", [400, 404, 422, 451])
def test_other_4xx_are_terminal(status):
    err = error_for_status(status)
    assert isinstance(err, TerminalError)
    assert err.backend_code == f"http_{status}"


def test_backend_code_and_message_overrides_win():
    err = error_for_status(404, backend_code="unknown_job", message="no such job")
    assert isinstance(err, TerminalError)
    assert err.backend_code == "unknown_job"
    assert "no such job" in str(err)


# ---- retry_after_seconds ----------------------------------------------------------------------


def test_retry_after_none_headers_returns_none():
    assert retry_after_seconds(None) is None


def test_retry_after_lowercase_and_capitalized_both_parse():
    assert retry_after_seconds({"retry-after": "5"}) == 5.0
    assert retry_after_seconds({"Retry-After": "5"}) == 5.0


def test_retry_after_absent_key_returns_none():
    assert retry_after_seconds({"content-type": "application/json"}) is None


def test_retry_after_unparseable_delta_returns_none():
    # HTTP-date form (e.g. "Wed, 21 Oct 2026 07:28:00 GMT") isn't handled in v0 → None, not a crash
    assert retry_after_seconds({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None


def test_retry_after_object_without_get_is_swallowed():
    # a headers-like value that has no .get() → AttributeError branch → None (never raises)
    assert retry_after_seconds([]) is None
