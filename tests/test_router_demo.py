"""Router demo — the compliance-first router driven over the REAL built-in adapter descriptors
(not synthetic fakes; test_router.py covers the router logic with fakes). This proves the actual
shipped `descriptor.compliance` facts route correctly: a PHI request routes to compliant backends
and refuses non-compliant ones, the fallback chain never relaxes compliance, a confirmed training
opt-out readmits Textract, and require_local collapses to the local floor — which then runs for
real end-to-end through the plan.
"""

from __future__ import annotations

from openreading.adapters.registry import build_registry
from openreading.types.request import OpenReadingRequest
from tests.fakes import make_backend

# Cloud backends whose real descriptors carry a BAA in force (hipaa_baa='yes') and a no-training
# posture, i.e. the ones allowed on a PHI path without running locally and without any operator
# confirmation. Reducto's BAA is tier_gated, so it belongs here only once confirmed.
_PHI_SAFE_CLOUD = {
    "azure-document-intelligence",
    "google-document-ai",
    "anthropic-claude",
}


def _phi_registry():
    """The real 9-adapter registry plus one deliberately non-compliant cloud backend that must
    never survive a PHI filter."""
    reg = build_registry()
    reg.register(make_backend("gemini-dev", hipaa_baa="no", trains="yes", forms=True, tables=True))
    return reg


def _req(**kw) -> OpenReadingRequest:
    body = {
        "document": {"path": "/doc.pdf", "mime_type": "application/pdf"},
        "backend": {"id": None},
    }
    body.update(kw)
    return OpenReadingRequest.model_validate(body)


def _is_phi_safe(desc, baa_tier_confirmed: frozenset[str] = frozenset()) -> bool:
    c = desc.compliance
    baa = c.hipaa_baa == "yes" or (c.hipaa_baa == "tier_gated" and desc.id in baa_tier_confirmed)
    return bool(c.runs_fully_local) or (baa and c.trains_on_customer_data in ("no", "na_local"))


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()
