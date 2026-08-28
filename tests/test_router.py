"""The 3-stage router, tested against the routing_and_compliance.md §4.6 worked examples and
the §5.3 compliance-never-relaxed invariant. Compliance is a hard filter; UNVERIFIED fails
closed; the fallback chain is drawn only from the surviving set; local is the guaranteed floor.
"""

from __future__ import annotations

import mimetypes

import pytest

from openreading.adapters.registry import build_registry, make_adapter
from openreading.batch.sources import normalize_input_format
from openreading.router import Registry, RouterConfig
from openreading.router.executor import execute_plan
from openreading.router.router import _QUALITY_BY_PRIORITY, _WEIGHTS, Router
from openreading.types import BackendType
from openreading.types.errors import ComplianceRefused
from openreading.types.request import OpenReadingRequest
from tests.fakes import make_backend


def _registry() -> Registry:
    """A realistic mini-registry mirroring the research's compliance/capability facts."""
    reg = Registry()
    # local tier (egress-free; survive every compliance filter)
    reg.register(
        make_backend(
            "pdfplumber", btype=BackendType.OSS_LIBRARY, local=True, tables=True, priority="P1"
        )
    )
    reg.register(
        make_backend(
            "pymupdf", btype=BackendType.OSS_LIBRARY, local=True, tables=True, priority="P0"
        )
    )
    reg.register(
        make_backend("tesseract", btype=BackendType.OSS_LIBRARY, local=True, priority="P1")
    )
    reg.register(
        make_backend(
            "qwen-vl",
            btype=BackendType.SELF_HOSTED_MODEL,
            local=True,
            handwriting=True,
            forms=True,
            tables=True,
            priority="P1",
        )
    )
    reg.register(
        make_backend(
            "docling",
            btype=BackendType.OSS_LIBRARY,
            local=True,
            tables=True,
            forms=True,
            priority="P0",
        )
    )
    # hosted, verified BAA + no-train
    reg.register(
        make_backend(
            "google-document-ai",
            hipaa_baa="yes",
            trains="no",
            regions=["us", "eu", "europe-west2"],
            max_retention_hours=24,
            handwriting=True,
            forms=True,
            tables=True,
            priority="P0",
            cost_low=0.0015,
            cost_high=0.03,
        )
    )
    reg.register(
        make_backend(
            "azure-document-intelligence",
            hipaa_baa="yes",
            trains="no",
            regions=["us", "eu"],
            max_retention_hours=24,
            handwriting=True,
            forms=True,
            tables=True,
            priority="P0",
            cost_low=0.0015,
            cost_high=0.01,
        )
    )
    # hosted, BAA but trains opt-out (needs confirmed opt-out to pass no-train/phi)
    reg.register(
        make_backend(
            "aws-textract",
            hipaa_baa="yes",
            trains="opt_out",
            regions=["us-east-1", "eu-west-1"],  # retention UNVERIFIED (None)
            handwriting=True,
            forms=True,
            tables=True,
            priority="P0",
            cost_low=0.0015,
            cost_high=0.07,
        )
    )
    # hosted, tier-gated BAA
    reg.register(
        make_backend(
            "reducto",
            hipaa_baa="tier_gated",
            trains="no",
            regions=["us"],
            max_retention_hours=0,
            handwriting=True,
            forms=True,
            tables=True,
            priority="P1",
            cost_low=0.015,
            cost_high=0.06,
        )
    )
    # hosted, NO baa + trains (must never survive a PHI/no-train filter)
    reg.register(
        make_backend(
            "gemini-dev",
            hipaa_baa="no",
            trains="yes",
            regions=["us"],
            handwriting=True,
            forms=True,
            tables=True,
            priority="P1",
            cost_low=0.0002,
            cost_high=0.005,
        )
    )
    # hosted, HIPAA-badge-only (BAA unverified => hipaa_baa "no") + trains unverified => "yes"
    reg.register(
        make_backend(
            "docsumo", hipaa_baa="no", trains="yes", forms=True, tables=True, priority="P2"
        )
    )
    # EU-native no-train
    reg.register(
        make_backend(
            "mindee",
            hipaa_baa="no",
            trains="no",
            regions=["eu"],
            max_retention_hours=0,
            forms=True,
            tables=True,
            priority="P1",
            cost_low=0.044,
            cost_high=0.044,
        )
    )
    return reg


def _req(**kw) -> OpenReadingRequest:
    body = {
        "document": {"path": "/doc.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "auto"},
    }
    body.update(kw)
    return OpenReadingRequest.model_validate(body)


# --- example (a): Medicare claim with PHI (handwriting + tables) -------------------------


def test_phi_handwriting_drops_noncompliant_and_text_only_backends():
    router = Router(_registry(), RouterConfig(train_optout_confirmed=frozenset()))
    req = _req(
        compliance={"require_baa": True, "no_train_on_data": True, "max_retention": "24h"},
        features={"handwriting": True, "forms_key_value": True},
    )
    plan = router.route(req)
    eligible = set(plan.eligible_ids)

    # stage 1: no BAA / trains-by-default never survive
    assert "gemini-dev" not in eligible
    assert "docsumo" not in eligible
    assert plan.dropped["gemini-dev"].stage == 1
    # aws-textract trains=opt_out and opt-out NOT confirmed => dropped at stage 1
    assert "aws-textract" not in eligible
    assert plan.dropped["aws-textract"].code == "trains_on_data"
    # stage 2: text-only local libs can't do handwriting
    assert "pdfplumber" not in eligible and "pymupdf" not in eligible
    assert plan.dropped["pdfplumber"].stage == 2
    # survivors: verified-BAA no-train clouds + local VLMs
    assert "google-document-ai" in eligible
    assert "azure-document-intelligence" in eligible
    assert "qwen-vl" in eligible  # local VLM: BAA-free PHI path
    # reducto's BAA is tier_gated and this deployment confirmed nothing => no BAA in force
    assert "reducto" not in eligible
    assert plan.dropped["reducto"].code == "no_baa"


def test_phi_confirmed_optout_readmits_textract():
    router = Router(_registry(), RouterConfig(train_optout_confirmed=frozenset({"aws-textract"})))
    req = _req(
        compliance={"require_baa": True, "no_train_on_data": True},
        features={"handwriting": True},
    )
    plan = router.route(req)
    assert "aws-textract" in plan.eligible_ids  # opt-out confirmed => passes no-train


# --- tier-gated BAA: a BAA offered on a higher plan is not a BAA in force ----------------
# routing_and_compliance.md §3.2 (Tier B): route PHI to a tier-gated backend only with the
# guardrail set. Mirrors trains_on_customer_data='opt_out' + train_optout_confirmed.


def test_tier_gated_baa_fails_closed_without_operator_confirmation():
    plan = Router(_registry()).route(_req(compliance={"require_baa": True}))
    assert "reducto" not in plan.eligible_ids
    dr = plan.dropped["reducto"]
    assert (dr.stage, dr.code) == (1, "no_baa")
    assert "tier_gated" in dr.detail and "baa_tier_confirmed" in dr.detail


def test_confirmed_tier_gated_baa_is_eligible_and_warns_on_the_response():
    reg = Registry()
    reg.register(make_backend("reducto", hipaa_baa="tier_gated", trains="no", regions=["us"]))
    req = _req(compliance={"require_baa": True})

    # unconfirmed, this is a terminal compliance-bounded failure, not a downgrade
    strict = Router(reg).route(req)
    assert strict.chosen is None and strict.terminal_reason == "no_compliant_backend"

    plan = Router(reg, RouterConfig(baa_tier_confirmed=frozenset({"reducto"}))).route(req)
    assert plan.eligible_ids == ["reducto"]
    resp = execute_plan(plan, req)
    notes = [w for w in (resp.warnings or []) if w.code == "baa_tier_confirmed"]
    assert len(notes) == 1
    assert notes[0].field == "reducto"
    assert "tier_gated" in (notes[0].message or "")


def test_tier_gated_confirmation_never_widens_the_eligible_set():
    req = _req(compliance={"require_baa": True})
    baseline = set(Router(_registry()).route(req).eligible_ids)
    confirmed = Router(_registry(), RouterConfig(baa_tier_confirmed=frozenset({"reducto"})))
    assert set(confirmed.route(req).eligible_ids) - baseline == {"reducto"}
    # confirming one backend says nothing about any other tier-gated or no-BAA backend
    elsewhere = Router(_registry(), RouterConfig(baa_tier_confirmed=frozenset({"chunkr"})))
    assert set(elsewhere.route(req).eligible_ids) == baseline


def test_tier_gated_carries_no_confirmation_note_when_require_baa_is_absent():
    plan = Router(_registry(), RouterConfig(baa_tier_confirmed=frozenset({"reducto"}))).route(
        _req(compliance={"no_train_on_data": True})
    )
    assert "reducto" in plan.eligible_ids  # require_baa absent => the BAA tier is irrelevant
    assert plan.baa_tier_notes == {}


def test_named_tier_gated_backend_is_refused_until_confirmed():
    req = _req(compliance={"require_baa": True})
    with pytest.raises(ComplianceRefused) as exc:
        Router(_registry()).check_eligible(req, "reducto")
    assert exc.value.constraint == "no_baa"
    confirmed = Router(_registry(), RouterConfig(baa_tier_confirmed=frozenset({"reducto"})))
    assert confirmed.check_eligible(req, "reducto").descriptor.id == "reducto"


def test_compliance_never_relaxed_by_fallback():
    router = Router(_registry())
    req = _req(
        compliance={"require_baa": True, "no_train_on_data": True}, features={"handwriting": True}
    )
    plan = router.route(req)
    # every stage-1 drop is absent from the ENTIRE chain (chosen + all fallbacks)
    stage1_dropped = {i for i, dr in plan.dropped.items() if dr.stage == 1}
    assert stage1_dropped, "expected some stage-1 drops"
    assert stage1_dropped.isdisjoint(set(plan.eligible_ids))


# --- routing.fallback: an explicit caller-supplied ordering, within the eligible set -----
# BL-43: `_apply_explicit_fallback` had zero coverage of any kind before this test — not only the
# duplicate-id bug, but the base case (does an explicit list reorder the chain at all).


def test_explicit_fallback_reorders_and_dedupes_the_chain():
    reg = Registry()
    reg.register(make_backend("a", local=True, priority="P0"))  # highest natural score
    reg.register(make_backend("b", local=True, priority="P1"))
    reg.register(make_backend("c", local=True, priority="P2"))  # lowest natural score
    router = Router(reg)

    # sanity: with no explicit fallback, the natural score order is a, b, c.
    natural = router.route(_req())
    assert [a.descriptor.id for a in natural.chain] == ["a", "b", "c"]

    # (a) base case, unverified before this test: a non-duplicate explicit routing.fallback list
    # actually moves those ids to the front, in the given order, ahead of the natural score order.
    reordered = router.route(_req(routing={"fallback": ["c", "a"]}))
    assert [a.descriptor.id for a in reordered.chain] == ["c", "a", "b"]

    # (b) BL-43's own bug: a duplicate id in routing.fallback must not occupy two chain slots.
    # Before the dict.fromkeys fix, "c" would appear as both plan.chosen and plan.fallbacks[0].
    deduped = router.route(_req(routing={"fallback": ["c", "c", "a"]}))
    assert deduped.chosen.descriptor.id == "c"
    assert [a.descriptor.id for a in deduped.fallbacks] == ["a", "b"]
    ids = [a.descriptor.id for a in deduped.chain]
    assert len(ids) == len(set(ids)), f"duplicate id resurfaced in the chain: {ids}"


# --- example (b): lender bank-statement batch (born-digital, no PHI, must_not_train) -----


def test_lender_batch_prefers_local_on_cost():
    router = Router(_registry())
    req = _req(
        compliance={"no_train_on_data": True},
        features={"tables": True},
        routing={"optimize_for": "cost"},
    )
    plan = router.route(req)
    # a local, no-train, zero-cost parser wins on cost weighting
    assert plan.chosen.descriptor.id in {"pymupdf", "pdfplumber", "docling", "tesseract", "qwen-vl"}
    assert plan.chosen.descriptor.compliance.runs_fully_local
    # gemini/docsumo (train) still excluded even though cheap
    assert "gemini-dev" not in plan.eligible_ids


# --- stage-3 cost scoring: a published 0.0 bound is a price, not an absence ---------------


def test_zero_cost_bound_is_a_price_not_an_absence():
    """A hosted backend with a free floor (low=0.0, high=0.10) must score its 0.05 midpoint.
    `lo or hi` read an explicit 0.0 as a missing bound and charged the full high end; only
    the runs_fully_local short-circuit kept today's descriptors off that path."""
    router = Router(Registry())
    req = _req()
    wq, wc = _WEIGHTS[None]
    quality_term = wq * _QUALITY_BY_PRIORITY["P1"]

    def cost_of(**bounds) -> float:
        desc = make_backend("b", priority="P1", **bounds).descriptor
        return (quality_term - router._score(req, desc)) / wc

    assert cost_of(cost_low=0.0, cost_high=0.10) == pytest.approx(0.05)
    assert cost_of(cost_low=0.10, cost_high=0.10) == pytest.approx(0.10)
    # a one-sided range still stands in for its missing end...
    assert cost_of(cost_high=0.10) == pytest.approx(0.10)
    assert cost_of(cost_low=0.0) == pytest.approx(0.0)
    # ...while a wholly unpriced backend keeps the unknown-price penalty.
    assert cost_of() == pytest.approx(0.05)


def test_free_floor_outranks_a_pricier_flat_rate_peer():
    reg = Registry()
    reg.register(make_backend("flat-rate", cost_low=0.08, cost_high=0.08))
    reg.register(make_backend("free-floor", cost_low=0.0, cost_high=0.10))
    plan = Router(reg).route(_req(routing={"optimize_for": "cost"}))
    # midpoints 0.05 < 0.08; reading the 0.0 floor as absent would price free-floor at 0.10.
    assert plan.chosen.descriptor.id == "free-floor"


# --- example (c): EU invoice under GDPR (region=eu, no PHI, no-train) --------------------


def test_eu_region_drops_us_only_backends():
    router = Router(_registry())
    req = _req(
        compliance={"data_region": "eu", "no_train_on_data": True},
        features={"forms_key_value": True},
    )
    plan = router.route(req)
    eligible = set(plan.eligible_ids)
    assert "mindee" in eligible  # EU no-train
    assert "google-document-ai" in eligible  # has eu region
    assert "docling" in eligible  # local, region-free
    # US-only + trains excluded
    assert "gemini-dev" not in eligible  # us-only region AND trains
    assert plan.dropped["gemini-dev"].stage == 1
    # reducto regions=["us"] only => region mismatch
    assert "reducto" not in eligible
    assert plan.dropped["reducto"].code == "region_mismatch"


def test_undeclared_neighbouring_region_drops_the_real_azure_descriptor():
    """`eastus2` is a real Azure region the shipped descriptor does not declare; it must not be
    satisfied by the declared `eastus` (or by the broad `us`) through string overlap."""
    reg = Registry()
    reg.register(make_adapter("azure-document-intelligence"))
    router = Router(reg)

    plan = router.route(_req(compliance={"data_region": "eastus2"}))
    assert plan.eligible_ids == []
    assert plan.chosen is None
    assert plan.terminal_reason == "no_compliant_backend"
    assert plan.dropped["azure-document-intelligence"].code == "region_mismatch"

    # a region the descriptor actually declares still routes
    ok = router.route(_req(compliance={"data_region": "eastus"}))
    assert ok.eligible_ids == ["azure-document-intelligence"]


# --- example (d): air-gapped / offline contract (require_local) -------------------------


def test_offline_only_drops_all_hosted_even_baa():
    router = Router(_registry())
    req = _req(compliance={"require_local": True}, features={"tables": True})
    plan = router.route(req)
    for a in plan.chain:
        assert a.descriptor.compliance.runs_fully_local
    # even a verified-BAA cloud is ineligible under require_local
    assert "google-document-ai" not in plan.eligible_ids
    assert plan.dropped["google-document-ai"].code == "not_local"


# --- invariants: unverified fail-closed, empty set, ComplianceRefused --------------------


def test_unverified_retention_fails_closed_but_can_be_allowed():
    strict = Router(_registry())
    req = _req(compliance={"require_baa": True, "max_retention": "24h"})
    # aws-textract retention is UNVERIFIED (None) => dropped when strict
    assert "aws-textract" not in strict.route(req).eligible_ids
    relaxed = Router(_registry(), RouterConfig(allow_unverified_compliance=True))
    assert "aws-textract" in relaxed.route(req).eligible_ids


def test_retention_exceeds_fires_for_a_well_formed_ceiling_against_a_disclosed_backend():
    # BL-44: a well-formed ceiling below a backend's KNOWN (disclosed) retention must drop it.
    # azure-document-intelligence discloses max_retention_hours=24; a 1h ceiling is exceeded.
    router = Router(_registry())
    plan = router.route(_req(compliance={"max_retention": "1h"}))
    assert "azure-document-intelligence" not in plan.eligible_ids
    assert plan.dropped["azure-document-intelligence"].code == "retention_exceeds"
    # a ceiling the disclosed retention actually satisfies still survives.
    ok = router.route(_req(compliance={"max_retention": "48h"}))
    assert "azure-document-intelligence" in ok.eligible_ids


@pytest.mark.parametrize("bad_retention", ["48hrs", "1 day", "24 hours"])
def test_unparseable_max_retention_fails_closed_unconditionally(bad_retention: str) -> None:
    # BL-44: an unparseable, non-empty max_retention must never silently pass a backend through —
    # not even one that DISCLOSES a retention figure, and not even under allow_unverified_compliance
    # (that flag governs tolerance for unverified BACKEND disclosure, not malformed CALLER input).
    req = _req(compliance={"max_retention": bad_retention})
    for router in (
        Router(_registry()),
        Router(_registry(), RouterConfig(allow_unverified_compliance=True)),
    ):
        plan = router.route(req)
        assert "azure-document-intelligence" not in plan.eligible_ids
        assert plan.dropped["azure-document-intelligence"].code == "retention_unparseable"


def test_region_unverified_drops_the_real_nuextract_descriptor():
    """nuextract discloses no region list at all (data_region_options=[]) — the
    data-residency axis's fail-closed twin of test_unverified_retention_fails_closed_..."""
    reg = Registry()
    reg.register(make_adapter("nuextract"))
    router = Router(reg)
    plan = router.route(_req(compliance={"data_region": "eu"}))
    assert plan.chosen is None
    assert plan.dropped["nuextract"].code == "region_unverified"


def test_region_unverified_can_be_allowed():
    reg = Registry()
    reg.register(make_adapter("nuextract"))
    relaxed = Router(reg, RouterConfig(allow_unverified_compliance=True))
    plan = relaxed.route(_req(compliance={"data_region": "eu"}))
    assert plan.eligible_ids == ["nuextract"]


def test_empty_eligible_set_is_terminal_not_a_downgrade():
    reg = Registry()
    reg.register(
        make_backend("gemini-dev", hipaa_baa="no", trains="yes")
    )  # only a non-compliant backend
    router = Router(reg)
    plan = router.route(_req(compliance={"require_baa": True}))
    assert plan.chosen is None
    assert plan.terminal_reason == "no_compliant_backend"


def test_check_eligible_raises_compliance_refused_for_named_backend():
    router = Router(_registry())
    req = _req(compliance={"require_baa": True})
    with pytest.raises(ComplianceRefused) as exc:
        router.check_eligible(req, "gemini-dev")
    assert exc.value.constraint == "no_baa"
    # a compliant named backend returns the adapter
    assert router.check_eligible(req, "google-document-ai").descriptor.id == "google-document-ai"


def test_local_is_the_guaranteed_floor_for_phi():
    router = Router(_registry())
    plan = router.route(_req(compliance={"require_baa": True, "require_local": True}))
    # require_local + require_baa => only local backends; chain non-empty (local floor)
    assert plan.chosen is not None
    assert all(a.descriptor.compliance.runs_fully_local for a in plan.chain)


# --- stage 2: the input-format gate's MIME -> format token -------------------------------

_MIME = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _declared_formats(adapter) -> set[str]:
    return {normalize_input_format(f) for f in adapter.descriptor.capabilities.input_formats}


@pytest.mark.parametrize("fmt", sorted(_MIME))
def test_shipped_registry_routes_every_mime_it_advertises(fmt):
    """Against the REAL registry, not a fake one: deriving the token by splitting the MIME on
    '/' and '.' yields 'document'/'sheet'/'presentation' for the OOXML family (and 'jpeg' for
    image/jpeg), which dropped every backend and made `chosen` None for a plain .docx run."""
    plan = Router(build_registry()).route(
        _req(document={"path": f"/doc.{fmt}", "mime_type": _MIME[fmt]})
    )
    assert plan.chosen is not None, f"{_MIME[fmt]} dropped every backend: {plan.dropped}"
    # every backend that advertises the format is eligible, and the gate still gates: no
    # backend with a format allow-list survives without declaring it.
    advertised = {a.descriptor.id for a in build_registry() if fmt in _declared_formats(a)}
    assert advertised, f"no built-in adapter declares {fmt}"
    assert advertised <= set(plan.eligible_ids)
    for adapter in plan.chain:
        if adapter.descriptor.capabilities.input_formats:
            assert fmt in _declared_formats(adapter)


def test_format_gate_still_drops_a_backend_that_lacks_the_format():
    reg = Registry()
    reg.register(make_backend("pdf-only", local=True, input_formats=["pdf"]))
    reg.register(make_backend("office", local=True, input_formats=["docx", "xlsx", "pptx"]))
    plan = Router(reg).route(_req(document={"path": "/d.docx", "mime_type": _MIME["docx"]}))
    assert plan.eligible_ids == ["office"]
    assert plan.dropped["pdf-only"].stage == 2
    assert plan.dropped["pdf-only"].code == "unsupported_format"


def test_ooxml_resolves_without_a_platform_mime_database(monkeypatch):
    # mimetypes only learned the OOXML family after 3.11 and a bare container has no
    # /etc/mime.types, so the docx/xlsx/pptx mapping must not depend on either.
    monkeypatch.setattr(mimetypes, "guess_extension", lambda mime: None)
    reg = Registry()
    reg.register(make_backend("office", local=True, input_formats=["docx", "xlsx", "pptx"]))
    for fmt in ("docx", "xlsx", "pptx"):
        plan = Router(reg).route(_req(document={"path": f"/d.{fmt}", "mime_type": _MIME[fmt]}))
        assert plan.eligible_ids == ["office"], fmt


def test_unrecognized_mime_falls_back_to_the_trailing_token():
    reg = Registry()
    reg.register(make_backend("xps-only", local=True, input_formats=["xps"]))
    plan = Router(reg).route(
        _req(document={"path": "/d.xps", "mime_type": "application/x-openreading-unknown.xps"})
    )
    assert plan.eligible_ids == ["xps-only"]
