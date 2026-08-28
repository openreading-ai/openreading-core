"""Capability resolution (DESIGN L6, D-v4-7). A backend is only faulted for a dimension it can
actually produce. Capability comes from two honest sources: what the subject's response actually
contains, and — when the backend id is known to the local registry — its descriptor's claims. An
unknown backend id degrades to `descriptor: False` with a `descriptor_unavailable` warning; we
never fabricate a capability we cannot substantiate.
"""

from __future__ import annotations

from typing import Any

from .ingest import Subject


def _claims(caps: Any, attr: str) -> bool:
    v = getattr(caps, attr, None)
    return bool(v) and v not in (False, "no", "none")


def _lookup_descriptor(backend_id: str) -> Any | None:
    """The registry descriptor for a backend id, or None if the id is unknown (or the adapter
    cannot be instantiated offline). Pure/local: no network, no credentials."""
    try:
        from openreading.adapters.registry import make_adapter

        return make_adapter(backend_id).descriptor
    except Exception:  # noqa: BLE001 — any failure to resolve degrades to "unknown", never raises
        return None


def resolve_capabilities(
    subjects: list[Subject],
) -> tuple[dict[str, dict[str, bool]], list[dict[str, Any]]]:
    """Per-subject capability map keyed by label, plus any `descriptor_unavailable` warnings.

    Each entry: {descriptor, fields, blocks, confidence}. `fields` is True if the subject produced
    any typed_fields OR its descriptor claims key-value / custom-schema extraction. `blocks` /
    `confidence` are seeded here for the M2 structural dimension.
    """
    caps: dict[str, dict[str, bool]] = {}
    warnings: list[dict[str, Any]] = []
    for s in subjects:
        desc = _lookup_descriptor(s.backend_id)
        produced_fields = bool(s.typed_fields)
        if desc is None:
            warnings.append(
                {
                    "code": "descriptor_unavailable",
                    "subject": s.label,
                    "detail": f"backend id {s.backend_id!r} is unknown to the local registry; "
                    "capability checks fall back to observed output",
                }
            )
            caps[s.label] = {
                "descriptor": False,
                "fields": produced_fields,
                # unknown → assume capable, so genuine misses still surface. Consumed by
                # blocks_section (BL-58) alongside its own per-subject output check, gating the
                # `table_cells` headline channel on genuine joint block-capability.
                "blocks": True,
                "confidence": True,
            }
            continue
        c = desc.capabilities
        caps[s.label] = {
            "descriptor": True,
            "fields": produced_fields
            or _claims(c, "forms_key_value")
            or _claims(c, "custom_schema_extraction"),
            "blocks": True,  # see the unknown-descriptor branch above — same BL-58 consumer
            "confidence": True,
        }
    return caps, warnings
