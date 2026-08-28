"""scrub_fixture — redact secrets from a captured payload BEFORE it is written to disk as a test
fixture (GOAL2 record mode). A recorded fixture must never contain an auth header, key, token,
presigned-URL signature, or account id. Safety over fidelity: over-redaction is fine (a corrupted
fixture is caught the next time the offline test replays it); a leaked secret is not.
"""

from __future__ import annotations

from typing import Any

PLACEHOLDER = "<scrubbed>"

# Keys whose VALUE is a secret (case- and separator-insensitive; '-'/'_' normalized).
_SECRET_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "x_api_key",
        "key",
        "token",
        "secret",
        "access_token",
        "client_secret",
        "password",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "signature",
        "svix_signature",
        "subscription_key",
        "ocp_apim_subscription_key",
        "webhook_secret",
        "credentials",
        "service_account",
        "private_key",
    }
)
# Query-string markers that flag a presigned/signed URL (its query is stripped).
_SIGNED_URL_MARKERS = ("sig=", "signature", "x-amz-", "x-goog-", "token=", "expires=", "se=")


def _is_secret_key(key: str) -> bool:
    k = str(key).lower().replace("-", "_")
    if k in _SECRET_KEYS:
        return True
    return (
        k.endswith(("_token", "_secret", "_key"))
        or "secret" in k
        or "password" in k
        or "account_id" in k
    )


def _scrub_str(s: str) -> str:
    if s.startswith(("http://", "https://")) and "?" in s:
        base, _, qs = s.partition("?")
        if any(m in qs.lower() for m in _SIGNED_URL_MARKERS):
            return base + "?" + PLACEHOLDER
    return s


def scrub_fixture(value: Any) -> Any:
    """Return a deep copy of `value` with secret-looking keys' values replaced by PLACEHOLDER and
    signed-URL query strings stripped. Recurses through dicts and lists."""
    if isinstance(value, dict):
        return {
            k: (PLACEHOLDER if _is_secret_key(k) else scrub_fixture(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [scrub_fixture(v) for v in value]
    if isinstance(value, str):
        return _scrub_str(value)
    return value
