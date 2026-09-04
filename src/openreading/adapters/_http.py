"""Shared httpx helpers for every adapter that speaks HTTP directly rather than through a vendor
SDK. This module supplies one client builder, Retry-After parsing, and the map from status code
to error taxonomy. httpx is an optional dependency (each adapter's extra plus the `http` extra)
and is imported lazily. Adapters still own their own request shapes, so nothing here decides what
a call looks like.
"""

from __future__ import annotations

from openreading.types.errors import RetryableError, TerminalError

# HTTP statuses that are transient and safe to retry the same backend on.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def build_httpx_client(*, base_url: str = "", headers: dict | None = None, timeout: float = 60.0):
    import httpx  # lazy: only when a real HTTP adapter runs

    # Fail fast, actionably: a non-empty base_url without a scheme would otherwise surface on the
    # FIRST REQUEST as httpx's cryptic "Request URL is missing an 'http://' or 'https://'
    # protocol" (seen live when a mis-parsed *_BASE_URL env override reached an adapter). Empty
    # stays valid — some adapters request absolute URLs.
    if base_url and not base_url.startswith(("http://", "https://")):
        raise TerminalError(
            f"backend base URL {base_url!r} must start with http:// or https://. Check the "
            f"backend's *_BASE_URL / endpoint configuration.",
            backend_code="bad_base_url",
        )
    return httpx.Client(base_url=base_url, headers=headers or {}, timeout=timeout)


def retry_after_seconds(headers) -> float | None:
    """Parse a Retry-After header (delta-seconds form) into seconds; None if absent/unparseable."""
    if headers is None:
        return None
    raw = None
    for k in ("retry-after", "Retry-After"):
        try:
            raw = headers.get(k)
        except AttributeError:
            raw = None
        if raw is not None:
            break
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None  # HTTP-date form is not parsed, so the caller falls back to its own backoff


def error_for_status(
    status_code: int, headers=None, *, backend_code: str | None = None, message: str = ""
):
    """Map an HTTP status to the error taxonomy. Returns an exception to raise (does not raise)."""
    # 401/403 = the key was found but rejected → a distinct terminal signal the CLI turns into a
    # "check <env var>" hint (never a retry, never a silent fall-through).
    if status_code in (401, 403):
        return TerminalError(message or f"HTTP {status_code}", backend_code="auth_rejected")
    # 413 = the document exceeds the provider's size/page limit → terminal, and the router MAY
    # fall back to a backend with a higher ceiling.
    if status_code == 413:
        return TerminalError(message or "document too large", backend_code="doc_too_large")
    code = backend_code or f"http_{status_code}"
    if status_code in _RETRYABLE_STATUS:
        return RetryableError(
            message or f"HTTP {status_code}",
            backend_code=code,
            retry_after=retry_after_seconds(headers),
        )
    return TerminalError(message or f"HTTP {status_code}", backend_code=code)
