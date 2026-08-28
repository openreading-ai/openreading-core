"""Router demo — the compliance-first router driven over the REAL built-in adapter descriptors
(not synthetic fakes; test_router.py covers the router logic with fakes). This proves the actual
shipped `descriptor.compliance` facts route correctly: a PHI request routes to compliant backends
and refuses non-compliant ones, the fallback chain never relaxes compliance, a confirmed training
opt-out readmits Textract, and require_local collapses to the local floor — which then runs for
real end-to-end through the plan.
"""

from __future__ import annotations

import pytest

from openreading.adapters.registry import build_registry
from openreading.router import RouterConfig
from openreading.router.clock import RealClock
from openreading.router.driver import run_to_completion
from openreading.router.router import Router
from openreading.types.errors import ComplianceRefused
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext
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
        "backend": {"id": "auto"},
    }
    body.update(kw)
    return OpenReadingRequest.model_validate(body)


def _is_phi_safe(desc, baa_tier_confirmed: frozenset[str] = frozenset()) -> bool:
    c = desc.compliance
    baa = c.hipaa_baa == "yes" or (c.hipaa_baa == "tier_gated" and desc.id in baa_tier_confirmed)
    return bool(c.runs_fully_local) or (baa and c.trains_on_customer_data in ("no", "na_local"))


def test_phi_routes_to_compliant_real_backends_and_refuses_noncompliant():
    router = Router(_phi_registry())  # opt-out NOT confirmed
    plan = router.route(_req(compliance={"require_baa": True, "no_train_on_data": True}))

    # a compliant backend is chosen, and the whole chain is PHI-safe (local, or verified BAA no-train)
    assert plan.chosen is not None
    for a in plan.chain:
        assert _is_phi_safe(a.descriptor), f"{a.descriptor.id} is not PHI-safe"

    eligible = set(plan.eligible_ids)
    # the injected non-compliant cloud (no BAA, trains) is refused at stage 1
    assert "gemini-dev" not in eligible
    assert plan.dropped["gemini-dev"].stage == 1
    # Textract trains=opt_out and the opt-out is not confirmed here -> dropped at stage 1
    assert "aws-textract" not in eligible
    assert plan.dropped["aws-textract"].code == "trains_on_data"
    # the real local parsers are the BAA-free PHI floor
    assert {"pymupdf", "tesseract", "docling", "qwen-vl"} <= eligible
    # the verified-BAA no-train clouds survive
    assert eligible >= _PHI_SAFE_CLOUD
    # reducto's real BAA is Growth+ tier-gated and nothing was confirmed here -> no BAA in force
    assert "reducto" not in eligible
    assert plan.dropped["reducto"].code == "no_baa"


def test_confirmed_baa_tier_readmits_real_reducto():
    confirmed = frozenset({"reducto"})
    router = Router(_phi_registry(), RouterConfig(baa_tier_confirmed=confirmed))
    plan = router.route(_req(compliance={"require_baa": True, "no_train_on_data": True}))
    assert "reducto" in plan.eligible_ids
    for a in plan.chain:
        assert _is_phi_safe(a.descriptor, confirmed), f"{a.descriptor.id} is not PHI-safe"
    # the confirmation is recorded on the plan, so the response can name what it rests on
    assert "tier_gated" in plan.baa_tier_notes["reducto"]
    # chunkr/pulse are tier_gated too and were NOT confirmed -> still closed
    assert {"chunkr", "pulse"}.isdisjoint(plan.eligible_ids)


def test_fallback_chain_never_relaxes_compliance():
    router = Router(_phi_registry())
    plan = router.route(_req(compliance={"require_baa": True, "no_train_on_data": True}))
    stage1_dropped = {i for i, dr in plan.dropped.items() if dr.stage == 1}
    assert stage1_dropped, "expected some stage-1 compliance drops"
    # not one stage-1-dropped backend reappears anywhere in the chosen+fallback chain
    assert stage1_dropped.isdisjoint(set(plan.eligible_ids))


def test_confirmed_optout_readmits_real_textract():
    router = Router(
        _phi_registry(), RouterConfig(train_optout_confirmed=frozenset({"aws-textract"}))
    )
    plan = router.route(_req(compliance={"require_baa": True, "no_train_on_data": True}))
    assert "aws-textract" in plan.eligible_ids  # confirmed opt-out passes the no-train gate


def test_named_noncompliant_backend_raises_compliance_refused():
    router = Router(_phi_registry())
    req = _req(compliance={"require_baa": True, "no_train_on_data": True})
    with pytest.raises(ComplianceRefused) as exc:
        router.check_eligible(req, "gemini-dev")
    assert exc.value.constraint in ("no_baa", "trains_on_data")


def test_require_local_collapses_to_local_floor_and_runs_end_to_end():
    pytest.importorskip("fitz", reason="pymupdf not installed")
    from openreading.testing.sample_pdf import build_sample_pdf

    router = Router(build_registry())
    req = _req(compliance={"require_local": True}, routing={"optimize_for": "offline"})
    plan = router.route(req)

    # only fully-local real adapters survive; every hosted one is dropped as not_local
    assert plan.chosen is not None
    for a in plan.chain:
        assert a.descriptor.compliance.runs_fully_local
    assert plan.dropped["google-document-ai"].code == "not_local"

    # the plan's chosen local backend actually runs the sample PDF to a schema-valid response
    from openreading import schemas

    run_req = OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": _b64(build_sample_pdf()),
                "mime_type": "application/pdf",
            },
            "backend": {
                "id": plan.chosen.descriptor.id,
                "type": plan.chosen.descriptor.type.value,
            },
        }
    )
    adapter = plan.chosen
    clock = RealClock()
    ctx = RunContext()
    job = adapter.submit(run_req, ctx)
    job = run_to_completion(
        adapter, job, ctx=ctx, deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    result = adapter.normalize(job, ctx, run_req).to_schema_dict()
    schemas.validate_response(result)
    assert result["document"]["pages"]


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode()
