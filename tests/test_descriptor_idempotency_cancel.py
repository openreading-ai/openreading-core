"""BL-166 + BL-164 — the shared `adapter-descriptor.v0.6.json` bump: `idempotency_supported` and
`cancel_supported`, both additive, both default `true`. BL-166 set `idempotency_supported`
honestly per adapter; BL-164 (landed later, same schema bump) sets `cancel_supported`.
"""

from __future__ import annotations

from openreading import schemas
from openreading.adapters.registry import BUILTIN_ADAPTERS


def _desc(**over) -> dict:
    body = {
        "id": "x",
        "type": "hosted_api",
        "provisioning": {"auth": "api_key"},
        "wait_modes": ["inline"],
        "capabilities": {"ocr": "verified"},
        "cost": {"native_unit": "page"},
        "compliance": {"hipaa_baa": "no"},
        "runtime": {"offline_capable": False},
    }
    body.update(over)
    return body


# This used to pin DESCRIPTOR_SCHEMA_FILE to v0.6. v0.7 (Ledger T4a — the optional
# `protocol_version` integer) is now current, and the "which file is current" pin moved with it to
# tests/test_descriptor_protocol_version.py::test_descriptor_v07_is_current — the established
# convention (see tests/test_liveness.py's own comment on the v0.5->v0.6 move) rather than simply
# disappearing.


def test_v06_descriptor_is_additive_a_v05_descriptor_still_validates():
    """The backward-compatibility guarantee in one assertion: adding idempotency_supported/
    cancel_supported must not invalidate a single existing (v0.5-shaped) descriptor."""
    schemas.validate_descriptor(_desc())


def test_descriptor_accepts_idempotency_and_cancel_flags():
    schemas.validate_descriptor(_desc(idempotency_supported=False, cancel_supported=True))


def test_pydantic_descriptor_defaults_both_flags_true():
    from openreading.types.descriptor import AdapterDescriptor

    desc = AdapterDescriptor.model_validate(_desc(protocol_version=1))
    assert desc.idempotency_supported is True
    assert desc.cancel_supported is True
    out = desc.to_schema_dict()
    assert out["idempotency_supported"] is True
    assert out["cancel_supported"] is True


def test_pydantic_descriptor_honors_an_explicit_false():
    from openreading.types.descriptor import AdapterDescriptor

    desc = AdapterDescriptor.model_validate(_desc(protocol_version=1, idempotency_supported=False))
    assert desc.idempotency_supported is False


def test_every_builtin_descriptor_validates_against_v06():
    """Every real, shipped adapter's descriptor is itself a valid v0.6 descriptor — not just the
    hand-built fixtures above."""
    for factory in BUILTIN_ADAPTERS.values():
        adapter = factory()
        schemas.validate_descriptor(adapter.descriptor.to_schema_dict())


# BL-166's own honesty matrix, researched per adapter against real vendor docs (see each
# adapter's own comment at its `idempotency_supported=` line for the citation). Pinned so a
# future refactor can't silently flip one back to the default `True` without a test catching it.
# `aws-textract` flipped to `True` in BL-165 — ClientRequestToken now works, since the S3 upload
# key is a pure function of content instead of a fresh uuid4 per attempt. The remaining seven —
# `reducto`/`anthropic-claude`/`azure-document-intelligence`/`chunkr`/`google-document-ai`/
# `nuextract`/`pulse` — have no vendor-side mechanism found at all.
_HONESTLY_UNSUPPORTED = frozenset(
    {
        "anthropic-claude",
        "azure-document-intelligence",
        "chunkr",
        "google-document-ai",
        "nuextract",
        "pulse",
        "reducto",
    }
)


def test_idempotency_supported_matrix_matches_researched_vendor_reality():
    for slug, factory in BUILTIN_ADAPTERS.items():
        supported = factory().descriptor.idempotency_supported
        if slug in _HONESTLY_UNSUPPORTED:
            assert supported is False, f"{slug} should honestly declare idempotency_supported=False"
        else:
            assert supported is True, f"{slug} should still default idempotency_supported=True"


# BL-164's own honesty matrix, researched per hosted adapter against real vendor docs (see each
# adapter's own comment at its `cancel_supported=` line for the citation). Local/self-hosted
# adapters (docling, pymupdf, qwen-vl, tesseract) are out of this item's fix surface — INLINE-only
# dispatch never holds a live, cancellable job by the time a `race`/`pick: best` node's loser-
# cancellation path could run — and keep the v0.6 default `True`, unresearched, the same
# categorical exemption BL-166 gave them for `idempotency_supported`.
#
# `chunkr`/`nuextract`/`pulse`/`reducto` have a genuine, working vendor cancel mechanism and a
# real adapter override that calls it (chunkr's is narrower — only effective while a task is
# still queued, not once it's actively processing — but the mechanism is real, not a no-op).
# `anthropic-claude`/`google-document-ai` decline for a DIFFERENT reason than the other three
# hosted decliners: their dispatch is INLINE-only, so there is never a live vendor job in flight
# to cancel in the first place — not "we researched and found no API," the same reasoning the
# local/self-hosted exemption above uses, just for a `hosted_api`-typed adapter.
_CANCEL_SUPPORTED = frozenset({"chunkr", "nuextract", "pulse", "reducto"})
_HOSTED_NO_CANCEL = frozenset(
    {
        "anthropic-claude",
        "aws-textract",
        "azure-document-intelligence",
        "google-document-ai",
        "open-ocr",
    }
)


def test_cancel_supported_matrix_matches_researched_vendor_reality():
    for slug, factory in BUILTIN_ADAPTERS.items():
        supported = factory().descriptor.cancel_supported
        if slug in _CANCEL_SUPPORTED:
            assert supported is True, f"{slug} should declare cancel_supported=True"
        elif slug in _HOSTED_NO_CANCEL:
            assert supported is False, f"{slug} should honestly decline cancel_supported"
        else:
            assert supported is True, (
                f"{slug} (local/self-hosted, out of BL-164's fix surface) should keep the v0.6 "
                "default True"
            )


def test_no_adapter_declaring_cancel_supported_false_overrides_cancel():
    # BL-164's own acceptance criterion: "an adapter declaring cancel_supported: false does not
    # silently claim success." An honest decliner must rely on BackendAdapter's own base
    # implementation (marks the job CANCELLED locally, never claims a vendor-side effect) rather
    # than a custom override that could quietly imply one.
    from openreading.adapters.base import BackendAdapter

    for slug in _HOSTED_NO_CANCEL:
        adapter = BUILTIN_ADAPTERS[slug]()
        assert type(adapter).cancel is BackendAdapter.cancel, (
            f"{slug} declares cancel_supported=False but overrides cancel() anyway"
        )


def test_every_cancel_supported_adapter_has_a_real_override():
    # The other half of the same honesty bar: a `True` claim must be backed by an actual
    # implementation, not just a flipped flag.
    from openreading.adapters.base import BackendAdapter

    for slug in _CANCEL_SUPPORTED:
        adapter = BUILTIN_ADAPTERS[slug]()
        assert type(adapter).cancel is not BackendAdapter.cancel, (
            f"{slug} declares cancel_supported=True but never overrides cancel()"
        )
