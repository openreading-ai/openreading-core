"""scrub_fixture + live-lane gating (GOAL2 10.1). Offline: proves a recorded fixture can never
carry a secret, and that live tests skip cleanly with the exact missing env var names."""

from __future__ import annotations

import pytest

from openreading.testing import scrub_fixture
from openreading.testing.scrub import PLACEHOLDER
from tests.live_helpers import gate_env, skip_unless_creds


def test_scrub_redacts_secret_keys():
    canary = "sk_CANARY_should_never_persist"
    payload = {
        "Authorization": f"Bearer {canary}",
        "api_key": canary,
        "aws_secret_access_key": canary,
        "webhook_secret": canary,
        "nested": {"x-api-key": canary, "svix-signature": canary},
        "list": [{"token": canary}],
    }
    scrubbed = scrub_fixture(payload)
    flat = str(scrubbed)
    assert canary not in flat  # the canary never survives, anywhere
    assert scrubbed["api_key"] == PLACEHOLDER
    assert scrubbed["nested"]["x-api-key"] == PLACEHOLDER
    assert scrubbed["list"][0]["token"] == PLACEHOLDER


def test_scrub_strips_presigned_url_query():
    payload = {
        "url": "https://s3.amazonaws.com/bucket/obj?X-Amz-Signature=abc123&X-Amz-Expires=3600",
        "plain": "https://example.com/doc.pdf",
    }
    scrubbed = scrub_fixture(payload)
    assert "abc123" not in str(scrubbed)
    assert scrubbed["url"] == "https://s3.amazonaws.com/bucket/obj?" + PLACEHOLDER
    assert scrubbed["plain"] == "https://example.com/doc.pdf"  # unsigned URL untouched


def test_scrub_preserves_non_secret_content():
    payload = {"markdown": "# Title", "page_count": 3, "segment_id": "seg_0", "confidence": 0.98}
    assert scrub_fixture(payload) == payload


def test_gate_env_uses_descriptor_live_gate_env():
    assert gate_env("reducto") == ["REDUCTO_API_KEY"]
    assert set(gate_env("google-document-ai")) == {
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GCP_PROJECT_ID",
        "GCP_PROCESSOR_ID",
    }


def test_skip_unless_creds_skips_when_missing(monkeypatch):
    monkeypatch.delenv("REDUCTO_API_KEY", raising=False)
    with pytest.raises(pytest.skip.Exception) as exc:
        skip_unless_creds("reducto")
    assert "REDUCTO_API_KEY" in str(exc.value)


def test_skip_unless_creds_passes_when_present(monkeypatch):
    monkeypatch.setenv("REDUCTO_API_KEY", "sk_present")
    skip_unless_creds("reducto")  # does not raise
