"""Milestone 15.1 — granularity: page (spec §2.7): per-page gates + pages.ranges escalation +
stitching with per-page source_backend. Fully offline: ScriptedBackends return multi-page output
with per-page confidence, and the escalation backend honors the request's pages.ranges.

The second half is fault injection on the rung loop: every way a page cascade can stop early
(unresolvable `auto`, a rung missing from the registry, a rung without page-range support, a
raising rung) must leave an honest trace, never a silent stop.
"""

from __future__ import annotations

import base64

import pytest

from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import CostBasis, CostReport, Job
from openreading.types.errors import (
    ComplianceRefused,
    PlanExhaustedError,
    RetryableError,
    TerminalError,
    UnsupportedFeatureError,
)
from openreading.types.request import OpenReadingRequest
from openreading.types.response import Page
from tests.fakes import ScriptedBackend, scripted_registry

GOOD = "the quick brown fox jumps over the lazy dog and it reads perfectly clean here "


class _BilledScriptedBackend(ScriptedBackend):
    """A ScriptedBackend whose `report_cost` reports a real `CostBasis.BILLED` basis (BL-120).
    The base `ScriptedBackend.report_cost` is hardcoded to `infra_only(...)` regardless of
    `cost_usd`, which can never exercise "a billed rung contributed" — this subclass makes that
    case reproducible without touching the shared fixture every other test relies on."""

    def report_cost(self, job: Job) -> CostReport:
        return CostReport(
            native_unit="page",
            native_quantity=1.0,
            cost_usd=self._cost_usd,
            basis=CostBasis.BILLED,
            billing_target="caller_account",
        )


class _EstimatedScriptedBackend(ScriptedBackend):
    """A ScriptedBackend whose `report_cost` reports `CostBasis.ESTIMATED` (BL-126) — the
    pricing-model-guess basis that must never let a cheaper `billed` rung's basis lose the
    priority-ordering reduction, regardless of which rung ran first or second."""

    def report_cost(self, job: Job) -> CostReport:
        return CostReport(
            native_unit="page",
            native_quantity=1.0,
            cost_usd=self._cost_usd,
            basis=CostBasis.ESTIMATED,
            billing_target="caller_account",
        )


def _req():
    return OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
                "mime_type": "application/pdf",
            },
            "backend": {"id": "strategy:s"},
        }
    )


def _run(cfg, reg):
    r = _req()
    compiled = compile_strategy(r, "s", StrategyConfig.model_validate(cfg), reg, RouterConfig())
    return run_strategy(compiled, r, registry=reg, broker=EnvCredentialBroker(), clock=FakeClock())


def _paged_cfg(steps=None):
    return {
        "version": 1,
        "strategies": {
            "s": {
                "granularity": "page",
                "steps": steps
                or [
                    {"backend": "pymupdf", "escalate_if": {"confidence_below": 0.80}},
                    "reducto",
                ],
                "budget": {"max_cost_usd": 1.0},
            }
        },
    }


def _cats(result):
    return [(a["backend"], a["category"]) for a in result.orchestration["attempts"]]


def _provenance(result):
    return {p["page"]: p["backend"] for p in result.orchestration["pages"]}


def _cheap_pages():
    # page 1 clean (conf 0.95), page 2 poor (conf 0.40 → escalates), page 3 clean (conf 0.90)
    return [
        Page(page_number=1, text=GOOD, confidence=0.95),
        Page(page_number=2, text="scrmbl", confidence=0.40),
        Page(page_number=3, text=GOOD, confidence=0.90),
    ]


def _strong_pages():
    # the strong backend re-parses page 2 cleanly (only page 2 is requested)
    return [
        Page(page_number=1, text=GOOD, confidence=0.99),
        Page(page_number=2, text="now perfectly legible", confidence=0.97),
        Page(page_number=3, text=GOOD, confidence=0.99),
    ]


def _forced_doc_pages():
    # a doc-granularity rung re-parses everything; its page 2 is still under the gate
    return [
        Page(page_number=1, text=GOOD, confidence=0.93),
        Page(page_number=2, text="still scrambled", confidence=0.55),
        Page(page_number=3, text=GOOD, confidence=0.94),
    ]


def test_page_granularity_escalates_only_failing_pages_and_stitches():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()),
        ScriptedBackend("reducto", cost_low=0.02, pages=_strong_pages(), page_ranges=True),
    )
    res = _run(_paged_cfg(), reg)
    pages = {p.page_number: p for p in res.response.document.pages or []}
    # pages 1 & 3 kept from the cheap rung; page 2 re-parsed by the strong rung
    assert pages[1].source_backend == "pymupdf"
    assert pages[3].source_backend == "pymupdf"
    assert pages[2].source_backend == "reducto"
    assert pages[2].text == "now perfectly legible"


def test_page_granularity_records_per_page_provenance():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()),
        ScriptedBackend("reducto", cost_low=0.02, pages=_strong_pages(), page_ranges=True),
    )
    res = _run(_paged_cfg(), reg)
    prov = {p["page"]: p["backend"] for p in res.orchestration["pages"]}
    assert prov == {1: "pymupdf", 2: "reducto", 3: "pymupdf"}


def test_page_granularity_only_page_2_is_re_parsed():
    strong = ScriptedBackend("reducto", cost_low=0.02, pages=_strong_pages(), page_ranges=True)
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()), strong)
    _run(_paged_cfg(), reg)
    # the strong backend saw exactly the failing page range (page 2)
    ranges = strong.requests[0].pages.ranges
    assert [(r.start, r.end) for r in ranges] == [(2, 2)]


def test_page_granularity_no_failures_skips_the_strong_rung():
    clean = [
        Page(page_number=1, text=GOOD, confidence=0.95),
        Page(page_number=2, text=GOOD, confidence=0.92),
    ]
    strong = ScriptedBackend("reducto", cost_low=0.02, pages=_strong_pages(), page_ranges=True)
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, pages=clean), strong)
    res = _run(_paged_cfg(), reg)
    assert len(strong.contexts) == 0  # every page passed → no escalation
    assert all(p.source_backend == "pymupdf" for p in res.response.document.pages or [])


def test_page_granularity_determinism():
    def once():
        reg = scripted_registry(
            ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()),
            ScriptedBackend("reducto", cost_low=0.02, pages=_strong_pages(), page_ranges=True),
        )
        res = _run(_paged_cfg(), reg)
        return res.orchestration["pages"]

    assert once() == once()


# ---- BL-120: honest money — every rung's real cost reaches the final response's usage ----------


def test_page_granularity_escalation_sums_the_escalated_rungs_billed_cost():
    # rung 1 (pymupdf) is free/local and gates page 2; rung 2 (reducto) is a real, billed rung
    # (cost_low= alone never reaches report_cost — an explicit cost_usd= is required so
    # _actual_cost sees a real number, per this item's own acceptance criteria).
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()),
        ScriptedBackend("reducto", cost_usd=0.30, pages=_strong_pages(), page_ranges=True),
    )
    res = _run(_paged_cfg(), reg)
    assert res.response.usage is not None
    assert res.response.usage.cost_usd == pytest.approx(0.30)


def test_page_granularity_no_escalation_reports_only_rung_1s_honest_cost():
    # every page passes the gate, so rung 2 never runs — its cost must never leak into the total.
    clean = [
        Page(page_number=1, text=GOOD, confidence=0.95),
        Page(page_number=2, text=GOOD, confidence=0.92),
    ]
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, cost_usd=0.05, pages=clean),
        ScriptedBackend("reducto", cost_usd=0.30, pages=_strong_pages(), page_ranges=True),
    )
    res = _run(_paged_cfg(), reg)
    assert res.response.usage is not None
    assert res.response.usage.cost_usd == pytest.approx(0.05)


def test_page_granularity_escalation_does_not_leave_cost_basis_claiming_infra_only():
    # base_resp is frozen to rung 1 (free/local, "infra_only"); once rung 2's real, billed spend
    # is folded into cost_usd, cost_basis must not go on silently asserting the run was free.
    strong = _BilledScriptedBackend(
        "reducto", cost_usd=0.30, pages=_strong_pages(), page_ranges=True
    )
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()), strong)
    res = _run(_paged_cfg(), reg)
    assert res.response.usage is not None
    assert res.response.usage.cost_usd == pytest.approx(0.30)
    assert res.response.usage.cost_basis == "billed"


# ---- BL-126: cost_basis folds by priority, not by which rung happened to run last --------------


def test_page_granularity_billed_rung_then_estimated_rung_keeps_billed_basis():
    # rung 1 is a real BILLED rung; rung 2 (escalated to) only reports an ESTIMATED basis. Before
    # this fix, _eval_paged_cascade's own last-write-wins line let the later, weaker-basis rung
    # silently downgrade a real vendor charge to a pricing-model guess.
    billed = _BilledScriptedBackend("pymupdf", cost_usd=0.30, local=True, pages=_cheap_pages())
    estimated = _EstimatedScriptedBackend(
        "reducto", cost_usd=0.05, pages=_strong_pages(), page_ranges=True
    )
    reg = scripted_registry(billed, estimated)
    res = _run(_paged_cfg(), reg)
    assert res.response.usage is not None
    assert res.response.usage.cost_usd == pytest.approx(0.35)
    assert res.response.usage.cost_basis == "billed"


def test_page_granularity_estimated_rung_then_billed_rung_keeps_billed_basis():
    # reversed order: an ESTIMATED rung 1 escalates to a BILLED rung 2. This ordering already
    # "self-corrected" under the old last-write-wins code (it's the escalated-to rung that always
    # wrote last) — pinned here so both orderings are covered, not only the one that used to fail.
    estimated = _EstimatedScriptedBackend(
        "pymupdf", cost_usd=0.05, local=True, pages=_cheap_pages()
    )
    billed = _BilledScriptedBackend(
        "reducto", cost_usd=0.30, pages=_strong_pages(), page_ranges=True
    )
    reg = scripted_registry(estimated, billed)
    res = _run(_paged_cfg(), reg)
    assert res.response.usage is not None
    assert res.response.usage.cost_usd == pytest.approx(0.35)
    assert res.response.usage.cost_basis == "billed"


# ---- BL-134: a rung with known cost but unset basis is folded as contributing nothing -----


def test_page_granularity_a_raising_report_cost_never_leaves_a_billed_rung_looking_free():
    # report_cost() can raise AFTER normalize() already set usage.cost_usd — the documented "an
    # adapter meters a channel itself" pattern (router/cost.py's own module docstring).
    # apply_cost_report's own except clause degrades gracefully but never runs merge_cost_report, so
    # usage.cost_basis stays unset on the escalated-to rung. Before this fix, _eval_paged_cascade's
    # own fold read that bare None straight through, so base_resp (rung 1, frozen) kept asserting
    # its own "infra_only" despite a real, nonzero total once rung 2's spend was folded in.
    strong = ScriptedBackend(
        "reducto",
        cost_usd=0.30,
        pages=_strong_pages(),
        page_ranges=True,
        report_cost_error=RuntimeError("meter exploded"),
    )
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()), strong)
    res = _run(_paged_cfg(), reg)
    assert res.response.usage is not None
    assert res.response.usage.cost_usd == pytest.approx(0.30)
    assert res.response.usage.cost_basis not in (None, "infra_only")


# ---- fault injection: every early exit from the rung loop leaves an honest trace ---------------


def test_page_granularity_unresolvable_auto_rung_stops_with_no_extra_attempt():
    # `auto` on the escalation rung with every eligible backend already attempted: the cascade
    # stops rather than re-running the rung it just escalated away from.
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()))
    steps = [{"backend": "pymupdf", "escalate_if": {"confidence_below": 0.80}}, "auto"]
    res = _run(_paged_cfg(steps), reg)
    assert _cats(res) == [("pymupdf", "succeeded")]
    assert _provenance(res) == {1: "pymupdf", 2: "pymupdf", 3: "pymupdf"}
    pages = {p.page_number: p for p in res.response.document.pages or []}
    assert pages[2].text == "scrmbl"  # the failing page is kept as-is, never dropped


def test_page_granularity_rung_missing_from_the_registry_records_provider_error():
    # the strategy names a real backend that this run's registry does not carry.
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()))
    res = _run(_paged_cfg(), reg)
    assert _cats(res) == [("pymupdf", "succeeded"), ("reducto", "error(provider_error)")]
    att = res.orchestration["attempts"][1]
    assert att["node"] == "root.steps[1]"
    assert att["detail"] == "not in registry"
    assert _provenance(res) == {1: "pymupdf", 2: "pymupdf", 3: "pymupdf"}


def test_page_granularity_first_rung_missing_from_the_registry_exhausts():
    # nothing ever produced a base response → the strategy fails; it does not return an empty doc.
    reg = scripted_registry(ScriptedBackend("reducto", cost_low=0.02, pages=_strong_pages()))
    steps = [{"backend": "pymupdf", "escalate_if": {"confidence_below": 0.80}}, "reducto"]
    with pytest.raises(PlanExhaustedError) as ei:
        _run(_paged_cfg(steps), reg)
    assert [t["category"] for t in ei.value.trail] == ["error(provider_error)"]


def test_page_granularity_rung_without_range_support_reparses_the_whole_doc_and_stops():
    # docling advertises no page_range_selection, so its rung runs document granularity — and a
    # doc-granularity rung ends the cascade even though its own page 2 would still fail the gate.
    doc_rung = ScriptedBackend("docling", local=True, pages=_forced_doc_pages())
    last = ScriptedBackend("reducto", cost_low=0.02, pages=_strong_pages(), page_ranges=True)
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()), doc_rung, last
    )
    steps = [
        {"backend": "pymupdf", "escalate_if": {"confidence_below": 0.80}},
        {"backend": "docling", "escalate_if": {"confidence_below": 0.80}},
        "reducto",
    ]
    res = _run(_paged_cfg(steps), reg)
    assert doc_rung.requests[0].pages is None  # no ranges sent: the rung saw the whole document
    assert _cats(res) == [("pymupdf", "succeeded"), ("docling", "succeeded")]
    assert len(last.contexts) == 0  # the third rung is never reached
    assert _provenance(res) == {1: "docling", 2: "docling", 3: "docling"}


@pytest.mark.parametrize(
    ("exc", "error_class"),
    [
        (TerminalError("5xx", backend_code="server"), "provider_error"),
        (RetryableError("slow down", backend_code="429"), "rate_limited"),
        (UnsupportedFeatureError("no pdf", feature="pdf"), "unsupported_feature"),
        (ComplianceRefused("no baa", constraint="require_baa"), "provider_error"),
    ],
)
def test_page_granularity_escalation_rung_error_keeps_the_cheap_pages(exc, error_class):
    strong = ScriptedBackend("reducto", cost_low=0.02, error=exc, page_ranges=True)
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()), strong)
    res = _run(_paged_cfg(), reg)
    assert _cats(res) == [("pymupdf", "succeeded"), ("reducto", f"error({error_class})")]
    assert _provenance(res) == {1: "pymupdf", 2: "pymupdf", 3: "pymupdf"}
    pages = {p.page_number: p for p in res.response.document.pages or []}
    assert pages[2].text == "scrmbl"  # the un-escalated page keeps the cheap rung's honest output
    assert [w.code for w in res.response.warnings or []] == ["fallback_used"]


def test_page_granularity_escalation_rung_plain_crash_keeps_the_cheap_pages_and_redacts(
    monkeypatch,
):
    # BL-99: the identical escalation-rung-failure shape as the four cases parametrized above, but
    # the one substitution none of them make — a plain, non-AdapterError exception (not a
    # TerminalError/RetryableError/UnsupportedFeatureError/ComplianceRefused) out of normalize().
    # _eval_paged_cascade's own `except _TAXONOMY` clause never matched it at all, so it propagated
    # straight out of the page cascade uncaught — the cheap rung's already-good pages, recovered
    # below, were never returned. `_eval_paged_cascade` doesn't record a rung error's message either
    # (only its class), so redaction is proven the same way the parallel-branch test is: the SAME
    # exception instance the rung caught is inspected directly, in place, after the call.
    canary = "sk_CANARY_paged_plain_9f33"
    monkeypatch.setenv("OPENREADING_TEST_PAGED_PLAIN_KEY", canary)
    crash = ValueError(f"malformed page structure, saw key={canary}")
    strong = ScriptedBackend(
        "reducto",
        cost_low=0.02,
        required_env=["OPENREADING_TEST_PAGED_PLAIN_KEY"],
        normalize_error=crash,
        page_ranges=True,
    )
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, pages=_cheap_pages()), strong)
    res = _run(_paged_cfg(), reg)
    assert _cats(res) == [("pymupdf", "succeeded"), ("reducto", "error(provider_error)")]
    pages = {p.page_number: p for p in res.response.document.pages or []}
    assert pages[2].text == "scrmbl"  # the un-escalated page keeps the cheap rung's honest output
    assert canary not in str(crash)
    assert "***" in str(crash)
