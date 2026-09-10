"""v0.2 descriptor BYO-credential declaration (GOAL2 milestone 6.1). Every adapter that needs
something from the environment declares a credentials_spec/config_spec whose keys match what its
code reads; hosted APIs carry a signup_url; secret vs non-secret is graded; and the v0.2 schema
is additive (a v0.1 descriptor still validates)."""

from __future__ import annotations

import pytest

from openreading import schemas
from openreading.adapters.registry import BUILTIN_ADAPTERS, build_registry, make_adapter
from openreading.types import ChannelGrade

# The exact credential/config keys each adapter reads (mirrors the adapter code; if the code
# changes what it reads, this and the spec must change together).
EXPECTED_CRED_KEYS = {
    "aws-textract": {"aws_access_key_id", "aws_secret_access_key", "aws_session_token"},
    "azure-document-intelligence": {"key"},
    "reducto": {"api_key", "webhook_secret"},
    "google-document-ai": {"credentials_path"},
    "google-gemini": {"api_key"},
    "anthropic-claude": {"api_key"},
    "qwen-vl": {"api_key"},
    "chunkr": {"api_key"},
    "pulse": {"api_key"},
    "nuextract": {"api_key"},
    "open-ocr": {"api_key"},
    "mistral-ocr": {"api_key"},
    "pymupdf": set(),
    "tesseract": set(),
    "docling": set(),
    "docling_local": set(),
}
EXPECTED_CONFIG_KEYS = {
    "aws-textract": {"region", "s3_bucket"},
    "azure-document-intelligence": {"endpoint"},
    "google-document-ai": {"project_id", "processor_id", "location"},
    "google-gemini": {"model"},
    "anthropic-claude": {"model"},
    "docling": {"endpoint"},
    "docling_local": {"assets_path", "tesseract_cmd", "tessdata_path"},
    "qwen-vl": {"endpoint", "model"},
    "chunkr": {"base_url"},
    "nuextract": {"base_url"},
    "open-ocr": {"engine"},
    "mistral-ocr": {"model"},
}
LOCAL_NO_CONFIG = {"pymupdf", "tesseract"}


def test_all_descriptors_validate_against_v02_schema():
    for adapter in build_registry():
        schemas.validate_descriptor(adapter.descriptor.to_schema_dict())


@pytest.mark.parametrize("slug", sorted(BUILTIN_ADAPTERS))
def test_spec_keys_match_expectation(slug):
    desc = make_adapter(slug).descriptor
    cred_keys = {c.key for c in desc.credentials_spec}
    config_keys = {c.key for c in desc.config_spec}
    assert cred_keys == EXPECTED_CRED_KEYS[slug], slug
    assert config_keys == EXPECTED_CONFIG_KEYS.get(slug, set()), slug
    # every spec field names at least one env var
    for f in [*desc.credentials_spec, *desc.config_spec]:
        assert f.env, f"{slug}:{f.key} names no env var"


def test_hosted_apis_declare_signup_url():
    for adapter in build_registry():
        desc = adapter.descriptor
        if desc.type.value == "hosted_api":
            assert desc.signup_url, desc.id


def test_secret_grading_is_honest():
    # keys/tokens ARE secret and live in credentials_spec.
    reducto = make_adapter("reducto").descriptor
    assert next(c for c in reducto.credentials_spec if c.key == "api_key").secret is True
    azure = make_adapter("azure-document-intelligence").descriptor
    assert next(c for c in azure.credentials_spec if c.key == "key").secret is True


def test_credentials_spec_holds_only_secrets():
    """the openreading.adapters runbook §3: endpoints, regions, and resource ids are ConfigFields. A non-secret
    CredentialField would route non-secret config through ctx.credentials — the split the env
    broker, the redactor, and the readiness UI all key off. The conformance kit enforces this too;
    this asserts it holds for every built-in."""
    for adapter in build_registry():
        offenders = [c.key for c in adapter.descriptor.credentials_spec if not c.secret]
        assert offenders == [], f"{adapter.descriptor.id}: {offenders} belong in config_spec"


def test_non_secret_config_reaches_ctx_runtime_not_ctx_credentials():
    """The point of the move: the broker resolves these into ctx.runtime, so `secret_values`
    (redaction) and `ctx.credentials` stay secrets-only."""
    from openreading.credentials import EnvCredentialBroker, build_run_context, secret_values
    from openreading.types.request import OpenReadingRequest

    env = {
        "GCP_PROJECT_ID": "proj",
        "GCP_PROCESSOR_ID": "proc",
        "GCP_LOCATION": "eu",
        "AWS_REGION": "eu-west-1",
        "AWS_ACCESS_KEY_ID": "AKIA",
        "AWS_SECRET_ACCESS_KEY": "shh",
    }
    broker = EnvCredentialBroker(env)

    def ctx_for(slug):
        desc = make_adapter(slug).descriptor
        req = OpenReadingRequest.model_validate(
            {"document": {"path": "/probe"}, "backend": {"id": slug}}
        )
        return desc, build_run_context(req, desc, broker=broker)

    gdai, gctx = ctx_for("google-document-ai")
    # google-document-ai's one credential field (credentials_path) is optional and this fixture's
    # env sets no GOOGLE_APPLICATION_CREDENTIALS, so nothing resolves — not because the adapter
    # declares no secrets (it does, since BL-155; see
    # test_google_document_ai_faults.py::test_bad_adc_path_is_redacted_once_auth_hinted_wraps_it).
    assert gctx.credentials is None
    assert gctx.runtime["project_id"] == "proj"
    assert gctx.runtime["processor_id"] == "proc"
    assert gctx.runtime["location"] == "eu"

    textract, tctx = ctx_for("aws-textract")
    assert tctx.runtime["region"] == "eu-west-1"
    assert tctx.credentials is not None and "region" not in tctx.credentials.values
    assert secret_values(textract, tctx.credentials) == {"AKIA", "shh"}
    assert secret_values(gdai, gctx.credentials) == set()


def test_pure_local_backends_declare_no_specs():
    for slug in LOCAL_NO_CONFIG:
        desc = make_adapter(slug).descriptor
        assert not desc.credentials_spec and not desc.config_spec
        assert desc.signup_url is None


def test_accepts_url_only_for_native_url_backends():
    accepts = {a.descriptor.id for a in build_registry() if a.descriptor.accepts_url}
    assert accepts == {
        "azure-document-intelligence",
        "reducto",
        "docling",
        "chunkr",
        "pulse",
        "open-ocr",
        "mistral-ocr",
    }


def test_live_gate_env_set_for_hosted_and_endpoint_backends():
    for adapter in build_registry():
        desc = adapter.descriptor
        needs = desc.provisioning.auth != "none" or any(
            m in desc.provisioning.byo_mode for m in ("endpoint", "container")
        )
        if needs:
            assert desc.live_gate_env, desc.id


def test_honesty_restoring_channel_grades_v03():
    """§6.5 honesty downgrades: a grade must describe what the adapter actually produces.
    anthropic emits the model's markdown and DERIVES plain text from it (text=D, not the N the
    audit flagged as a lie). qwen's markdown is native only in markdown-mode and derived in
    html-mode; the static grade is the honest floor D, not N (§3.3 mode-dependence)."""
    anthropic = make_adapter("anthropic-claude").descriptor.output.channels
    assert anthropic.markdown is ChannelGrade.NATIVE
    assert anthropic.text is ChannelGrade.DERIVABLE

    qwen = make_adapter("qwen-vl").descriptor.output.channels
    assert qwen.markdown is ChannelGrade.DERIVABLE
    assert qwen.text is ChannelGrade.DERIVABLE


def test_descriptor_sources_cite_nothing_from_the_company_repo():
    """A descriptor's `sources[]` is public: it reaches users verbatim through
    `descriptor.to_schema_dict()` and `GET /v1/backends`. AGENTS.md sanctions an `internal/<path>`
    reference in a comment or docstring, where the reader is an agent editing this repo; on the
    wire it is a citation nobody outside the company can resolve, and it advertises the private
    repo's layout to every caller. The rationale itself belongs in the comment beside the field it
    justifies — only the pointer is dropped."""
    for adapter in build_registry():
        desc = adapter.descriptor
        for src in desc.to_schema_dict().get("sources", []):
            url = src.get("url") or ""
            assert not url.startswith("internal/"), f"{desc.id} cites a private path: {url}"
