"""Schema-evolution meta-tests (DESIGN §7/§8) — the machinery that makes an accidental breaking
change impossible to ship silently:

* BACKWARD_TRANSITIVE: every released version's golden fixture validates against its own and
  every NEWER response schema, under the version-field substitution rule.
* Forward-tolerance: the response envelope parses unknown future fields and survives
  re-serialization (extra is not "forbid").
* Non-additive schema-diff gate: released vendored schema files are byte-frozen.
* Response-additivity twin: a response carrying none of the v0.3 fields still validates against
  v0.3 (the twin of the descriptor additivity test).
* x-stability registry agreement: the generated experimental-field registry equals the schema
  annotations.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema.validators import validator_for

from openreading import schemas
from openreading.types import NormalizedResponse

SCHEMA_DIR = Path(schemas.__file__).parent
GOLDEN_DIR = Path(__file__).parent / "golden" / "response"

# Released response versions, oldest first. v0.3 is the in-progress (unreleased) cut and is NOT
# frozen: §6 permits editing an unreleased file during the milestone it ships in.
RELEASED_RESPONSE = ["v0.1", "v0.2", "v0.3"]


def _response_schema(version: str) -> dict:
    return json.loads((SCHEMA_DIR / f"response.{version}.json").read_text())


def _validate(schema: dict, instance: dict) -> None:
    validator_for(schema)(schema).validate(instance)


# --------------------------------------------------------------- BACKWARD_TRANSITIVE
def test_golden_fixture_exists_per_released_response_version():
    for v in RELEASED_RESPONSE:
        assert (GOLDEN_DIR / f"{v}.json").exists(), f"missing golden response fixture for {v}"


@pytest.mark.parametrize("golden_version", RELEASED_RESPONSE)
def test_backward_transitive_golden_validates_against_every_newer_schema(golden_version):
    """A frozen golden from an older version must validate against its own and every newer
    response schema. The substitution rule (§7) rewrites schema_version to the target schema's
    const first — schema_version is a per-version const, so a literal old value can never match a
    newer const. This is what proves additive evolution never broke an older instance."""
    golden = json.loads((GOLDEN_DIR / f"{golden_version}.json").read_text())
    start = RELEASED_RESPONSE.index(golden_version)
    for target in RELEASED_RESPONSE[start:]:
        schema = _response_schema(target)
        target_const = schema["properties"]["schema_version"]["const"]
        probe = {**golden, "schema_version": target_const}  # documented substitution rule
        _validate(schema, probe)


def test_backward_transitive_never_edits_old_goldens_or_schemas():
    """Guard: the substitution happens on a COPY; the golden files and old schemas are read-only."""
    before = {p.name: p.read_bytes() for p in GOLDEN_DIR.glob("*.json")}
    test_backward_transitive_golden_validates_against_every_newer_schema("v0.1")
    after = {p.name: p.read_bytes() for p in GOLDEN_DIR.glob("*.json")}
    assert before == after


# --------------------------------------------------------------- forward-tolerance gate
def test_response_envelope_tolerates_unknown_future_fields():
    """A synthetic instance with an unknown top-level field must parse under the current pydantic
    model and survive re-serialization (§7 forward-tolerance; §8 unknown-field tolerance is
    contract). 'ignore' drops the unknown so the re-serialized output stays schema-valid."""
    base = {
        "schema_version": "0.3",
        "status": {"state": "succeeded"},
        "backend": {"id": "future-backend", "type": "hosted_api"},
        "document": {"text": "hello"},
        "some_field_from_a_newer_version": {"nested": [1, 2, 3]},
    }
    obj = NormalizedResponse.model_validate(base)  # must NOT raise
    out = obj.to_schema_dict()
    assert "some_field_from_a_newer_version" not in out  # dropped, not fabricated
    schemas.validate_response(out)  # survives re-serialization as a valid response


# --------------------------------------------------------------- non-additive schema-diff gate
# sha256 of every RELEASED vendored schema file. Editing any of them (immutable per §8) flips this
# test red. Unreleased v0.3 files are intentionally absent — they may still change this milestone.
# Re-pinned once, 2026-08-18: the openmanifold -> openreading project rename rewrote $id (domain
# openmanifold.dev -> openreading.ai), title, and the `billing_target` enum value in place across
# every released file. Deliberate identity change, not schema evolution — pre-1.0, never published
# to PyPI, so no consumer had pinned the old $ids. §8 immutability resumes from these digests.
# Re-pinned a second time, 2026-08-29, for request.v0.1.json alone: the `require_baa` field's own
# `description` said the gate admits "backends with a signed-BAA path". The router cannot see any
# agreement an organisation signed; it reads what a vendor publishes about its own BAA. Every prose
# page was corrected to "advertises a BAA, necessary and not sufficient" during the documentation
# repair, and this string was the last site still telling a reader the opposite in the file
# AGENTS.md names as the source of truth. Validation behaviour is byte-for-byte unchanged (a
# description is inert), so this is a documentation correction inside a schema rather than schema
# evolution, and a new version would have made every consumer migrate for a sentence.
# request.v0.2.json frozen at cut, 2026-08-31 (M12, security review): additionalProperties:false
# now closes every nested object node, not only the top level, matching the pydantic mirrors'
# extra="forbid" (openreading.types.request). request.v0.1.json is untouched and stays frozen
# above — this is a new file, not an edit to the old one.
_FROZEN_RELEASED = {
    "request.v0.1.json": "c3fdc3ee4e3fcf5eddc3a7d6be4ef3fb938479f1f982f47d0f72a6b2e4f42a26",
    "request.v0.2.json": "31a32a3b1d1c59a76066477b2565bab39a5fdc197cbbdaf850fb3e7b5d181ad4",
    "response.v0.1.json": "41a1506091027f894508327b50d27f47205dd689f9d5dcfc48b3375ea19d0772",
    "response.v0.2.json": "aad609e0ad1e1d09891bd1a19649b352532ef12443dfc44cc08342cefa9f6d69",
    "adapter-descriptor.v0.1.json": "fb5f5f366ae06c8f8c536e1a2300b8a5b17387308050e4ac0c2e4b7ae03818f8",
    "adapter-descriptor.v0.2.json": "ca7ae59c46e1fad2d216ec30139f1899d630a33b5fd1d9d94fb1a91f6fb5449f",
    "comparison-report.v0.1.json": "ad7cb3154bd6dc4083682f7f356cd33e34c8c898297be77b5fb63f8a6953d4c3",
    "strategy-config.v0.1.json": "1080ed4b28d0455766ed5789535c8d4d075eb73b78d167d18c79ed582cd5c0b2",
}
_UNRELEASED = {
    "response.v0.3.json",
    "adapter-descriptor.v0.3.json",
    "comparison-report.v0.2.json",
    # Manifest v0.6 — new/in-progress this milestone (not yet byte-frozen):
    "adapter-descriptor.v0.4.json",
    "batch-result.v0.1.json",
    "corpus-report.v0.1.json",
    # Plain v0.7 — new/in-progress this milestone (freeze at milestone end):
    "strategy-config.v0.2.json",
    # One file — `policy` closed to its nine typed keys (freeze at milestone end):
    "strategy-config.v0.3.json",
    "strategy-config.v0.4.json",
    # Leaderboard (BL-160) — brand-new family this milestone (not yet byte-frozen):
    "leaderboard-report.v0.1.json",
    # The removal set — batch intake dispatches every source, so `skip_reason`, the `skipped`
    # item state and `summary.skipped` all leave:
    "batch-result.v0.2.json",
    # The removal set — compliance, optimize_for and `auto` leave the request; the compliance
    # profile and the priority hints leave the descriptor; `policy` becomes one key:
    "request.v0.3.json",
    "adapter-descriptor.v0.7.json",
    "adapter-descriptor.v0.8.json",
    # Pulse (internal/design/liveness.md) — brand-new family + the additive descriptor bump that
    # carries its optional `liveness` block (freeze both at milestone end):
    "liveness-report.v0.1.json",
    "adapter-descriptor.v0.5.json",
    # BL-166 + BL-164 — additive descriptor bump carrying idempotency_supported/cancel_supported
    # (one bump for both defects; not yet byte-frozen):
    "adapter-descriptor.v0.6.json",
    # Ledger T1 (internal/design/ledger.md §5.4) — brand-new families this milestone, first shipped by
    # T1 (not yet byte-frozen):
    "step.v0.1.json",
    "journal.v0.1.json",
    "local-document.v0.1.json",
    "passage.v0.1.json",
    "agent-document-tool.v0.1.json",
    # Ledger T4a (AC-8) — additive descriptor bump carrying the optional `protocol_version`
    # integer (not yet byte-frozen):
}


@pytest.mark.parametrize("name,digest", sorted(_FROZEN_RELEASED.items()))
def test_released_schema_files_are_byte_frozen(name, digest):
    actual = hashlib.sha256((SCHEMA_DIR / name).read_bytes()).hexdigest()
    assert actual == digest, (
        f"{name} is a RELEASED (immutable) schema file but its bytes changed — cut a new version "
        f"instead of editing it in place (§8)."
    )


def test_every_versioned_schema_file_is_either_frozen_or_unreleased():
    """No released version can slip in unpinned: every vendored vX.Y.json is either in the frozen
    set or explicitly on the unreleased list."""
    for p in SCHEMA_DIR.glob("*.v*.json"):
        assert p.name in _FROZEN_RELEASED or p.name in _UNRELEASED, (
            f"{p.name} is neither frozen-released nor listed unreleased — pin it or list it."
        )


# --------------------------------------------------------------- response-additivity twin
def test_response_additivity_a_pre_v0_3_response_still_validates():
    """Twin of the descriptor additivity test: a response using none of the v0.3-added fields
    (document.confidence / channel_provenance / schema_url) must remain valid under v0.3."""
    pre_v03 = {
        "schema_version": "0.3",
        "status": {"state": "succeeded"},
        "backend": {"id": "pymupdf", "type": "oss_library"},
        "document": {
            "text": "hello",
            "pages": [{"page_number": 1, "blocks": [{"type": "text", "text": "hello"}]}],
        },
        "orchestration": {"strategy": "x"},  # a v0.2 field, still fine
    }
    schemas.validate_response(pre_v03)
    for forbidden in ("confidence", "doc_type"):
        assert forbidden not in pre_v03["document"]
    assert "channel_provenance" not in pre_v03 and "schema_url" not in pre_v03


# --------------------------------------------------------------- request nested-strictness parity (M12)
def _walk_object_nodes(node: dict, path: str, skip_subtrees: frozenset[str]):
    """Yield (path, node) for every JSON-Schema object node (``type: object`` + ``properties``)
    reachable from `node`, depth-first, without following `$ref`/`$defs` (the request schema is
    built entirely inline, unlike response). `path` is dotted for a property and suffixed `[]` for
    array items. Recursion stops at any path listed in `skip_subtrees`: that value is a caller-
    supplied blob the wire contract does not own, not a field of the request shape itself."""
    if path in skip_subtrees:
        return
    if node.get("type") == "object" and "properties" in node:
        yield path, node
    for name, sub in (node.get("properties") or {}).items():
        if isinstance(sub, dict):
            yield from _walk_object_nodes(sub, f"{path}.{name}" if path else name, skip_subtrees)
    items = node.get("items")
    if isinstance(items, dict):
        yield from _walk_object_nodes(items, f"{path}[]", skip_subtrees)


def test_request_schema_extraction_schema_value_stays_open():
    """Twin of the structural walk above: `extraction_schema.json_schema` is deliberately EXCLUDED
    from the additionalProperties sweep because its value is an arbitrary caller-supplied JSON
    Schema (routed to the backend's native extraction mechanism), not a field this contract
    defines the shape of. Pin that the node exists and carries no `additionalProperties` /
    `properties` of its own, so a future edit cannot accidentally close it back up."""
    node = schemas.request_schema()["properties"]["extraction_schema"]["properties"]["json_schema"]
    assert node.get("type") == "object"
    assert "properties" not in node and "additionalProperties" not in node


# --------------------------------------------------------------- x-stability registry agreement
def test_x_stability_registry_matches_schema_annotations():
    """The generated registry (§8) and the raw schema annotations must agree — neither can drift
    without the other. channel_provenance is the one experimental field in v0.3."""
    registry = schemas.experimental_fields()
    assert registry == {"channel_provenance"}

    # cross-check against the raw annotation so a rename of the field or the keyword is caught
    resp = schemas.response_schema()
    assert resp["properties"]["channel_provenance"]["x-stability"] == "experimental"
    # nothing STABLE is mislabelled experimental
    assert "text" not in registry and "schema_version" not in registry
