"""Ledger T4a (AC-8) — the `adapter-descriptor.v0.7.json` bump: the optional `protocol_version`
integer on the wire schema, and the pydantic `AdapterDescriptor` model's own, stricter requirement
that every in-process construction declare it explicitly (no default).
"""

from __future__ import annotations

import pydantic
import pytest

from openreading import schemas
from openreading.adapters.registry import BUILTIN_ADAPTERS
from openreading.types.descriptor import AdapterDescriptor


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


def test_descriptor_v07_is_current():
    # the "which file is current" pin migrates forward with each bump (established convention —
    # see tests/test_descriptor_idempotency_cancel.py's own comment on the v0.6->v0.7 move).
    assert schemas.DESCRIPTOR_SCHEMA_FILE == "adapter-descriptor.v0.7.json"


def test_v07_descriptor_is_additive_a_v06_descriptor_still_validates():
    """The backward-compatibility guarantee in one assertion: adding `protocol_version` to the
    WIRE schema must not invalidate a single existing (v0.6-shaped, no protocol_version)
    descriptor — the JSON schema deliberately leaves it optional (see the field's own description
    in adapter-descriptor.v0.7.json) even though the pydantic model requires it."""
    schemas.validate_descriptor(_desc())


def test_descriptor_accepts_a_protocol_version():
    schemas.validate_descriptor(_desc(protocol_version=2))


def test_pydantic_descriptor_requires_protocol_version_explicitly():
    """AC-8: 'declares... by name' — a silent default would let a future adapter skip declaring it
    entirely, so the pydantic model (unlike the wire schema) has no default at all."""
    with pytest.raises(pydantic.ValidationError):
        AdapterDescriptor.model_validate(_desc())  # no protocol_version → must raise


def test_pydantic_descriptor_honors_an_explicit_protocol_version():
    desc = AdapterDescriptor.model_validate(_desc(protocol_version=2))
    assert desc.protocol_version == 2
    assert desc.to_schema_dict()["protocol_version"] == 2


def test_every_builtin_descriptor_declares_a_protocol_version():
    """Every real, shipped adapter's descriptor declares protocol_version explicitly (construction
    itself would already have raised otherwise — this is belt-and-suspenders documentation that
    the invariant holds for the actual registry, not just a hand-built fixture)."""
    for factory in BUILTIN_ADAPTERS.values():
        adapter = factory()
        assert adapter.descriptor.protocol_version in (1, 2)
        schemas.validate_descriptor(adapter.descriptor.to_schema_dict())
