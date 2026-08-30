"""Milestone 11.5 — the serial cascade engine (execution.md §1-2, integration.md §2).

Drives the engine with ScriptedBackends + FakeClock (offline, deterministic). Covers the
Outcome laws (accept / escalate / keep-best), classify_error, on_error routing, credential skip,
auto-leaf attempted-set, the compile/prune pipeline, and the orchestration trace.
"""

from __future__ import annotations

import pytest

from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.executor import execute_plan
from openreading.router.router import RoutePlan, RouterConfig
from openreading.strategies import StrategyConfig, classify_error, compile_strategy, run_strategy
from openreading.strategies.engine import on_error_action
from openreading.types import CostBasis, CostReport, Job
from openreading.types.errors import (
    ComplianceRefused,
    PlanExhaustedError,
    RetryableError,
    TerminalError,
    UnsupportedFeatureError,
)
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry

CLEAN = "The quick brown fox jumps over the lazy dog, and it does this every single day here. " * 4
GARBLED = "Ã©Ã¨ÃªÃ«Å â€™Ã±Â§Â¶ Ã Ã¢Ã¤ Ãµ Ã¼Ã¿ â‚¬Â£Â¥ Ã˜Ã† Ã‡Ã‰ " * 4


class _BilledScriptedBackend(ScriptedBackend):
    """A ScriptedBackend whose `report_cost` reports a real `CostBasis.BILLED` basis (BL-126). The
    base `ScriptedBackend.report_cost` is hardcoded to `infra_only(...)` regardless of `cost_usd`,
    which can never exercise "a billed rung contributed" — this subclass makes that case
    reproducible without touching the shared fixture every other test relies on."""

    def report_cost(self, job: Job) -> CostReport:
        return CostReport(
            native_unit="page",
            native_quantity=1.0,
            cost_usd=self._cost_usd,
            basis=CostBasis.BILLED,
            billing_target="caller_account",
        )


def _req(compliance=None, fallback=None):
    body = {
        "document": {"path": "/x.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "strategy:s"},
    }
    if compliance:
        body["compliance"] = compliance
    if fallback:
        body["routing"] = {"fallback": fallback}
    return OpenReadingRequest.model_validate(body)


def _run(cfg_dict, name, registry, *, req=None, router_config=None, broker=None):
    cfg = StrategyConfig.model_validate(cfg_dict)
    r = req or _req()
    compiled = compile_strategy(r, name, cfg, registry, router_config or RouterConfig())
    return run_strategy(
        compiled,
        r,
        registry=registry,
        broker=broker or EnvCredentialBroker(),
        clock=FakeClock(),
    )


def _cats(result):
    return [(a["backend"], a["category"]) for a in result.orchestration["attempts"]]


# ---- honest money: a cascade sums every billed rung ------------------------------------------


def test_cascade_usage_sums_escalated_and_winning_rungs():
    # the cheap rung escalates (billed 0.05) and the premium rung wins (billed 0.10); the returned
    # response's usage.cost_usd must be the TOTAL 0.15, not just the winner's own cost.
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN, confidence=0.5, cost_usd=0.05),
        ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN, confidence=0.95, cost_usd=0.10),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [
                    {"backend": "reducto", "escalate_if": {"confidence_below": 0.85}},
                    "aws-textract",
                ],
                "budget": {"max_cost_usd": 1.0},
            }
        },
    }
    res = _run(cfg, "s", reg)
    assert res.response.backend.id == "aws-textract"
    assert res.response.usage.cost_usd == pytest.approx(0.15)  # 0.05 escalated + 0.10 winner


def test_cascade_folds_billed_rungs_into_a_composite_terminal_step():
    # a cheap leaf escalates (billed 0.05), then a nested-cascade step wins (its own cost 0.10); the
    # returned total must include BOTH — the outer billed rung is not dropped by the composite return.
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN, confidence=0.5, cost_usd=0.05),
        ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN, cost_usd=0.10),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [
                    {"backend": "reducto", "escalate_if": {"confidence_below": 0.85}},
                    {"steps": ["aws-textract"]},  # nested-cascade (composite) terminal step
                ],
                "budget": {"max_cost_usd": 1.0},
            }
        },
    }
    res = _run(cfg, "s", reg)
    assert res.response.backend.id == "aws-textract"
    assert res.response.usage.cost_usd == pytest.approx(0.15)  # 0.05 escalated + 0.10 composite


# ---- BL-126: honest money — usage.cost_basis reconciles alongside cost_usd --------------------


def test_cascade_reconciles_cost_basis_when_a_billed_rung_escalates_to_a_free_winner():
    # the escalated-away rung is a real BILLED cost; the winner is free/local. cost_usd already
    # summed the escalated spend (BL-120) — before this fix, _set_total_cost never touched
    # cost_basis at all, so it silently kept the free winner's own "infra_only", asserting
    # something weaker than what actually happened (a real vendor charge occurred).
    billed = _BilledScriptedBackend("reducto", cost_usd=0.05, text=CLEAN, confidence=0.5)
    reg = scripted_registry(
        billed, ScriptedBackend("pymupdf", local=True, text=CLEAN, confidence=0.95)
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [
                    {"backend": "reducto", "escalate_if": {"confidence_below": 0.85}},
                    "pymupdf",
                ],
                "budget": {"max_cost_usd": 1.0},
            }
        },
    }
    res = _run(cfg, "s", reg)
    assert res.response.backend.id == "pymupdf"
    assert res.response.usage.cost_usd == pytest.approx(0.05)
    assert res.response.usage.cost_basis == "billed"


def test_cascade_reconciles_cost_basis_across_a_composite_terminal_step():
    # the escalated leaf is a real BILLED cost; the composite terminal step's own leaf reports
    # "infra_only" (ScriptedBackend.report_cost's own hardcoded default). cost_usd already summed
    # both (BL-120) — cost_basis must not silently prefer the composite's own weaker basis just
    # because it ran last and wrote the returned response object.
    billed = _BilledScriptedBackend("reducto", cost_usd=0.05, text=CLEAN, confidence=0.5)
    reg = scripted_registry(
        billed, ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN, cost_usd=0.10)
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [
                    {"backend": "reducto", "escalate_if": {"confidence_below": 0.85}},
                    {"steps": ["aws-textract"]},  # nested-cascade (composite) terminal step
                ],
                "budget": {"max_cost_usd": 1.0},
            }
        },
    }
    res = _run(cfg, "s", reg)
    assert res.response.backend.id == "aws-textract"
    assert res.response.usage.cost_usd == pytest.approx(0.15)  # 0.05 escalated + 0.10 composite
    assert res.response.usage.cost_basis == "billed"


def test_cascade_reconciles_cost_basis_at_best_effort_exhausted():
    # pymupdf (free/local) is retained as best-so-far; reducto also escalates, contributing a real
    # BILLED cost, before azure-di's hard error ends the walk — on_quality_exhausted defaults to
    # "best_effort", returning pymupdf's own deficient result. cost_usd already summed reducto's
    # spend into it (BL-120) — cost_basis must not silently keep pymupdf's own "infra_only" once a
    # real vendor charge has been folded into the total.
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=GARBLED),
        _BilledScriptedBackend("reducto", cost_usd=0.05, text=GARBLED),
        ScriptedBackend(
            "azure-di", cost_low=0.01, error=TerminalError("boom", backend_code="server")
        ),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {"steps": ["pymupdf", "reducto", "azure-di"], "escalate_if": "default"}
            },
        },
        "s",
        reg,
    )
    assert res.response.backend.id == "pymupdf"  # first retained best-so-far (equal-quality tie)
    assert res.orchestration["outcome"] == "degraded"
    assert res.response.usage.cost_usd == pytest.approx(0.05)  # reducto's escalated spend only
    assert res.response.usage.cost_basis == "billed"


# ---- BL-134: a rung with known cost but unset basis is folded as contributing nothing -----


def test_cascade_a_raising_report_cost_never_leaves_a_billed_rung_looking_free():
    # report_cost() can raise AFTER normalize() already set usage.cost_usd — the documented "an
    # adapter meters a channel itself" pattern (router/cost.py's own module docstring).
    # apply_cost_report's own except clause degrades gracefully but never runs merge_cost_report, so
    # usage.cost_basis stays unset on that rung. Before this fix, _eval_cascade's own leaf-accept
    # fold read that bare None straight through, and the escalated rung's own genuine "infra_only"
    # tag outranked the coalesced "unknown" by priority — asserting the run was free despite a real,
    # nonzero total.
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN, confidence=0.5, cost_usd=0.05),
        ScriptedBackend(
            "aws-textract",
            cost_low=0.01,
            text=CLEAN,
            confidence=0.95,
            cost_usd=0.10,
            report_cost_error=RuntimeError("meter exploded"),
        ),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [
                    {"backend": "reducto", "escalate_if": {"confidence_below": 0.85}},
                    "aws-textract",
                ],
                "budget": {"max_cost_usd": 1.0},
            }
        },
    }
    res = _run(cfg, "s", reg)
    assert res.response.backend.id == "aws-textract"
    assert res.response.usage.cost_usd == pytest.approx(0.15)  # 0.05 escalated + 0.10 winner
    assert res.response.usage.cost_basis not in (None, "infra_only")


# ---- classify_error (spec §5.1 table) ---------------------------------------------------------


@pytest.mark.parametrize(
    "exc,expected",
    [
        (UnsupportedFeatureError("x"), "unsupported_feature"),
        (RetryableError("x", backend_code="timeout"), "timeout"),
        (RetryableError("x", backend_code="rate_limited"), "rate_limited"),
        (RetryableError("x", backend_code="whatever"), "provider_error"),
        (TerminalError("x", backend_code="auth_rejected"), "auth"),
        (TerminalError("x", backend_code="corrupt_document"), "invalid_input"),
        (TerminalError("x", backend_code="doc_too_large"), "unsupported_feature"),
        (TerminalError("x", backend_code="weird"), "provider_error"),
        (TerminalError("x"), "provider_error"),
    ],
)
def test_classify_error_table(exc, expected):
    assert classify_error(exc) == expected


# ---- cascade Outcome laws ---------------------------------------------------------------------


def test_first_step_accepted_when_clean():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, text="unused"),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"}},
        },
        "s",
        reg,
    )
    assert res.response.backend.id == "pymupdf"
    assert res.orchestration["outcome"] == "ok"
    assert _cats(res) == [("pymupdf", "succeeded")]


def test_escalation_on_garbled_quality():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=GARBLED),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"}},
        },
        "s",
        reg,
    )
    assert res.response.backend.id == "reducto"
    assert _cats(res) == [("pymupdf", "quality_escalated"), ("reducto", "succeeded")]
    assert any(w.code == "quality_escalated" for w in (res.response.warnings or []))
    # the escalated attempt records its fired gates
    gates = res.orchestration["attempts"][0]["gates"]
    assert any(g["predicate"] == "garbled" and g["fired"] for g in gates)


def test_keep_best_when_all_escalate_and_final_errors():
    # pymupdf escalates (garbled), reducto (final, gated explicitly) errors -> keep pymupdf deficient
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=GARBLED),
        ScriptedBackend(
            "reducto", cost_low=0.01, error=TerminalError("boom", backend_code="server")
        ),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"}},
        },
        "s",
        reg,
    )
    assert res.response.backend.id == "pymupdf"  # the retained deficient result
    assert res.orchestration["outcome"] == "degraded"
    assert any(w.code == "quality_below_threshold" for w in (res.response.warnings or []))


def test_on_quality_exhausted_fail_raises():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=GARBLED),
        ScriptedBackend(
            "reducto", cost_low=0.01, error=TerminalError("boom", backend_code="server")
        ),
    )
    with pytest.raises(PlanExhaustedError):
        _run(
            {
                "version": 1,
                "strategies": {
                    "s": {
                        "steps": ["pymupdf", "reducto"],
                        "escalate_if": "default",
                        "on_quality_exhausted": "fail",
                    }
                },
            },
            "s",
            reg,
        )


# ---- on_error routing -------------------------------------------------------------------------


def test_invalid_input_fails_the_cascade_by_default():
    reg = scripted_registry(
        ScriptedBackend(
            "pymupdf", local=True, error=TerminalError("corrupt", backend_code="corrupt_document")
        ),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),
    )
    with pytest.raises(PlanExhaustedError) as ei:
        _run({"version": 1, "strategies": {"s": {"steps": ["pymupdf", "reducto"]}}}, "s", reg)
    # never advanced to reducto — a corrupt doc fails on every backend
    assert [t["category"] for t in ei.value.trail] == ["error(invalid_input)"]


def test_transient_error_advances():
    reg = scripted_registry(
        ScriptedBackend(
            "reducto", cost_low=0.01, error=TerminalError("5xx", backend_code="server")
        ),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )
    res = _run({"version": 1, "strategies": {"s": {"steps": ["reducto", "pymupdf"]}}}, "s", reg)
    assert res.response.backend.id == "pymupdf"
    assert _cats(res) == [("reducto", "error(provider_error)"), ("pymupdf", "succeeded")]


def test_on_error_fail_override_stops_chain():
    reg = scripted_registry(
        ScriptedBackend(
            "reducto", cost_low=0.01, error=TerminalError("5xx", backend_code="server")
        ),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )
    with pytest.raises(PlanExhaustedError):
        _run(
            {
                "version": 1,
                "strategies": {"s": {"steps": ["reducto", "pymupdf"], "on_error": {"any": "fail"}}},
            },
            "s",
            reg,
        )


def test_step_on_error_overrides_the_cascade_map_via_the_transient_alias():
    # the cascade advances on anything; the failing step stops on any transient class (§5.3). Proves
    # the step map is the most-specific argument at the leaf call site, not just at the unit level.
    reg = scripted_registry(
        ScriptedBackend(
            "reducto", cost_low=0.01, error=TerminalError("5xx", backend_code="server")
        ),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [{"backend": "reducto", "on_error": {"transient": "fail"}}, "pymupdf"],
                "on_error": {"any": "next"},
            }
        },
    }
    with pytest.raises(PlanExhaustedError):
        _run(cfg, "s", reg)


# ---- on_error_action resolution (unit — spec §5.1/§5.2) ---------------------------------------

TRANSIENT_CLASSES = ["timeout", "rate_limited", "provider_error"]
NON_TRANSIENT_CLASSES = ["auth", "unsupported_feature", "invalid_input", "budget_exhausted"]


@pytest.mark.parametrize("cls", TRANSIENT_CLASSES)
def test_on_error_action_exact_class_key_wins_within_a_map(cls):
    # "Specific class keys override `any`" (§5.2) — and the `transient` alias with it.
    assert on_error_action(cls, {cls: "fail", "transient": "next", "any": "next"}) == "fail"
    assert on_error_action(cls, {cls: "next", "transient": "fail", "any": "fail"}) == "next"


@pytest.mark.parametrize("cls", TRANSIENT_CLASSES)
def test_on_error_action_transient_alias_covers_each_transient_class(cls):
    # alias: transient = timeout + rate_limited + provider_error (§5.1)
    assert on_error_action(cls, {"transient": "fail"}) == "fail"
    assert on_error_action(cls, {"transient": "fail", "any": "next"}) == "fail"


@pytest.mark.parametrize("cls", NON_TRANSIENT_CLASSES)
def test_on_error_action_transient_alias_does_not_capture_other_classes(cls):
    assert on_error_action(cls, {"transient": "fail", "any": "next"}) == "next"
    # with no `any` to catch it, a non-transient class falls through to its class default
    assert on_error_action(cls, {"transient": "fail"}) == on_error_action(cls)


@pytest.mark.parametrize(
    "cls,expected",
    [
        ("timeout", "next"),
        ("rate_limited", "next"),
        ("auth", "next"),
        ("provider_error", "next"),
        ("unsupported_feature", "next"),
        ("invalid_input", "fail"),  # a corrupt document fails on every backend (§5.1)
        ("exhausted", "next"),
        ("budget_exhausted", "next"),
        ("not_a_class", "next"),  # unknown class → advance, the conservative direction
    ],
)
def test_on_error_action_class_defaults(cls, expected):
    assert on_error_action(cls) == expected
    assert on_error_action(cls, None, {}) == expected  # absent/empty maps are transparent


def test_on_error_action_any_is_the_within_map_catch_all():
    assert on_error_action("auth", {"any": "fail"}) == "fail"
    assert on_error_action("invalid_input", {"any": "next"}) == "next"  # overrides the fail default


def test_on_error_action_most_specific_map_decides_first():
    # the engine calls (step map, cascade map) — the step overrides the cascade (§5.3)
    assert on_error_action("timeout", {"timeout": "fail"}, {"timeout": "next"}) == "fail"
    assert on_error_action("timeout", {"transient": "fail"}, {"timeout": "next"}) == "fail"
    assert on_error_action("auth", {"any": "fail"}, {"auth": "next"}) == "fail"
    # a map that says nothing about the class is transparent — the next map decides
    assert on_error_action("timeout", {"auth": "fail"}, {"timeout": "fail"}) == "fail"
    assert on_error_action("auth", {"transient": "fail"}, {"auth": "next"}) == "next"


# ---- credentials ------------------------------------------------------------------------------


def test_missing_credentials_skips():
    reg = scripted_registry(
        ScriptedBackend(
            "reducto", cost_low=0.01, required_env=["OPENREADING_TEST_NEVERSET_KEY"], text=CLEAN
        ),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )
    res = _run({"version": 1, "strategies": {"s": {"steps": ["reducto", "pymupdf"]}}}, "s", reg)
    assert res.response.backend.id == "pymupdf"
    assert _cats(res) == [("reducto", "skipped(missing_credentials)"), ("pymupdf", "succeeded")]


# ---- auto leaf + attempted set ----------------------------------------------------------------


def test_auto_leaf_picks_untried_eligible():
    # steps [pymupdf, auto]: pymupdf escalates, auto must pick reducto (not re-pick pymupdf)
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=GARBLED),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"steps": ["pymupdf", "auto"], "escalate_if": "default"}},
        },
        "s",
        reg,
    )
    assert res.response.backend.id == "reducto"
    assert [a["backend"] for a in res.orchestration["attempts"]] == ["pymupdf", "reducto"]


# ---- compile / prune pipeline -----------------------------------------------------------------


def test_compile_prunes_noncompliant_backend():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),  # non-local
    )
    req = _req(compliance={"require_local": True})
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"}},
        },
        "s",
        reg,
        req=req,
    )
    # reducto pruned before execution; only pymupdf remains, and it's recorded in dropped[]
    dropped = {d["backend"] for d in res.orchestration.get("dropped", [])}
    assert "reducto" in dropped
    assert res.response.backend.id == "pymupdf"


def test_fully_pruned_root_refuses():
    reg = scripted_registry(ScriptedBackend("reducto", cost_low=0.01, text=CLEAN))  # only non-local
    req = _req(compliance={"require_local": True})
    with pytest.raises(ComplianceRefused):
        _run({"version": 1, "strategies": {"s": ["reducto"]}}, "s", reg, req=req)


def test_routing_fallback_overridden_warns():
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN))
    req = _req(fallback=["reducto"])
    res = _run({"version": 1, "strategies": {"s": ["pymupdf"]}}, "s", reg, req=req)
    assert any(w.code == "strategy_overrides_fallback" for w in (res.response.warnings or []))


def test_routing_fallback_desugars_to_escalate_off_cascade():
    """spec §7 rule 7: a request's `routing.fallback: [a, b]` IS the desugared serial cascade
    `{steps: [a, b], escalate_if: off}` — the same first-fails→advance walk. The legacy
    `execute_plan` chain and the equivalent strategy pick the same backend and return the same
    document (today's chain is a point in the design, not a second engine)."""
    err = TerminalError("boom", backend_code="server")
    plain_req = OpenReadingRequest.model_validate(
        {"document": {"path": "/d.pdf"}, "backend": {"id": "auto"}}
    )
    # legacy fallback chain: a fails → b wins
    legacy = execute_plan(
        RoutePlan(
            chosen=ScriptedBackend("a", cost_low=0.01, error=err),
            fallbacks=[ScriptedBackend("b", local=True, text=CLEAN)],
        ),
        plain_req,
        broker=EnvCredentialBroker(),
    )
    # equivalent desugared cascade
    reg = scripted_registry(
        ScriptedBackend("a", cost_low=0.01, error=err),
        ScriptedBackend("b", local=True, text=CLEAN),
    )
    strat = _run(
        {"version": 1, "strategies": {"s": {"steps": ["a", "b"], "escalate_if": "off"}}}, "s", reg
    )
    assert legacy.backend.id == strat.response.backend.id == "b"
    assert legacy.document.text == strat.response.document.text == CLEAN


def test_reference_resolves():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=GARBLED),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {"steps": ["pymupdf", "strategy:hosted"], "escalate_if": "default"},
            "hosted": ["reducto"],
        },
    }
    res = _run(cfg, "s", reg)
    assert res.response.backend.id == "reducto"


# ---- trace / provenance -----------------------------------------------------------------------


def test_orchestration_block_shape():
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN))
    res = _run({"version": 1, "strategies": {"s": ["pymupdf"]}}, "s", reg)
    o = res.orchestration
    assert o["strategy"] == "s"
    assert o["config_hash"].startswith("sha256:")
    assert o["chosen_backend"] == "pymupdf"
    assert o["outcome"] == "ok"
    assert isinstance(o["attempts"], list) and o["decisions"] == []


def test_response_stays_v01_schema_valid():
    from openreading import schemas

    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN))
    res = _run({"version": 1, "strategies": {"s": ["pymupdf"]}}, "s", reg)
    schemas.validate_response(res.response.to_schema_dict())  # orchestration is returned separately


# ---- BL-99: a plain normalize() crash in a parallel branch ------------------------------------


def test_parallel_branch_recovers_from_a_plain_normalize_crash_and_redacts_it():
    # _run_branch's own `except (TerminalError, RetryableError, UnsupportedFeatureError,
    # ComplianceRefused)` clause (execution.md §3) never matched a plain, non-AdapterError exception
    # out of normalize() — it isn't one of the five _ADAPTER_ERRORS taxonomy types. Uncaught, it
    # propagated out of the branch's asyncio.Task and blew up the whole parallel node, not just the
    # one losing branch. `_BranchOutcome`/the trace's own aggregation never carries a branch error's
    # message (only `error_class`), so redaction is proven the same way
    # test_execute_plan_redacts_a_plain_normalize_crash_and_falls_back does: the SAME exception
    # instance the branch caught is inspected directly, in place, after the call.
    canary = "sk_CANARY_branch_plain_9f32"
    crash = ValueError(f"malformed page structure, saw key={canary}")
    reg = scripted_registry(
        ScriptedBackend(
            "reducto",
            cost_low=0.01,
            required_env=["OPENREADING_TEST_BRANCH_PLAIN_KEY"],
            normalize_error=crash,
        ),
        ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN),
    )
    broker = EnvCredentialBroker({"OPENREADING_TEST_BRANCH_PLAIN_KEY": canary})
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["reducto", "aws-textract"],
                    "pick": "fastest",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        "s",
        reg,
        broker=broker,
    )
    assert res.response.backend.id == "aws-textract"  # the healthy branch still won, not a crash
    assert canary not in str(crash)
    assert "***" in str(crash)


# ---- a blown time budget is not a quality problem (B9) -----------------------------------------
# Both endings retain a result and both are `outcome: degraded`, but they call for opposite
# responses: a gated-out result says "the document was hard, escalate"; a deadline overrun says
# "you ran out of time, do not spend more of it". Emitting one code for both made the count of
# runs that hit their time budget unrecoverable, and pointed the documented triage action
# ("escalate to a stronger backend") at the one case where escalating is the worst answer.


class _SlowScriptedBackend(ScriptedBackend):
    """A rung that burns real wall time inside `submit`, so a real clock crosses a real deadline.
    `test_latency_ms` is a parallel-branch knob only — a cascade rung has no virtual latency."""

    def __init__(self, *args, sleep_s: float = 0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._sleep_s = sleep_s

    def submit(self, req, ctx):
        import time as _time

        _time.sleep(self._sleep_s)
        return super().submit(req, ctx)


def _run_realtime(cfg_dict, name, registry):
    from openreading.router.clock import RealClock

    cfg = StrategyConfig.model_validate(cfg_dict)
    r = _req()
    compiled = compile_strategy(r, name, cfg, registry, RouterConfig())
    return run_strategy(
        compiled, r, registry=registry, broker=EnvCredentialBroker(), clock=RealClock()
    )


def _deadline_cfg():
    return {
        "version": 1,
        "strategies": {
            "s": {
                "steps": ["pymupdf", "reducto"],
                "escalate_if": "default",
                "budget": {"max_duration": "10ms"},
            }
        },
    }


def test_deadline_overrun_emits_budget_exhausted_not_a_quality_warning():
    reg = scripted_registry(
        _SlowScriptedBackend("pymupdf", local=True, text=GARBLED, sleep_s=0.08),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),
    )
    res = _run_realtime(_deadline_cfg(), "s", reg)

    assert res.orchestration["outcome"] == "degraded"  # still degraded, still keep-best
    assert ("reducto", "succeeded") not in _cats(res)  # the deadline really stopped the walk
    codes = {w.code for w in (res.response.warnings or [])}
    assert "budget_exhausted" in codes
    assert "quality_below_threshold" not in codes  # the two causes are not aliased


def test_a_genuine_quality_exhaustion_still_says_quality_below_threshold():
    # the control: same keep-best ending, no deadline involved — the existing signal is unchanged
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=GARBLED),
        ScriptedBackend(
            "reducto", cost_low=0.01, error=TerminalError("boom", backend_code="server")
        ),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"}},
        },
        "s",
        reg,
    )
    codes = {w.code for w in (res.response.warnings or [])}
    assert "quality_below_threshold" in codes
    assert "budget_exhausted" not in codes


def test_a_failing_rung_records_the_backend_s_own_error_code(monkeypatch):
    """A29: with the `tesseract` binary off PATH, a `try: [tesseract, pymupdf]` strategy exits 0,
    `outcome: ok`, warning `fallback_used`, and the attempt reads `error(provider_error)` — the
    same category a rate-limit or a network blip gets, though a missing local binary is permanent
    and will fail identically on every run until someone installs it.

    The engine's error CLASS is a closed, schema-versioned set with an `on_error` routing contract
    (`strategy-config` `$defs.on_error`, `additionalProperties: false`), and no uniform, cheap way
    exists to tell "permanent host fault" from "transient provider fault" without branching on
    backend type — the one thing the router is forbidden to do. So the class stays
    `provider_error`, and what ships instead is the discriminator the adapter already computed and
    the trace was throwing away: `TerminalError.backend_code`, recorded as the attempt's `code`
    exactly as a compliance drop records its own. `detail` is prose for a human; `code` is what a
    monitor groups by."""
    reg = scripted_registry(
        ScriptedBackend(
            "tesseract",
            local=True,
            error=TerminalError(
                "tesseract failed: tesseract is not installed or it's not in your PATH.",
                backend_code="TesseractNotFoundError",
            ),
        ),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )
    res = _run({"version": 1, "strategies": {"s": {"steps": ["tesseract", "pymupdf"]}}}, "s", reg)

    failed = res.orchestration["attempts"][0]
    assert failed["category"] == "error(provider_error)"
    assert failed["code"] == "TesseractNotFoundError"
    # the succeeding rung carries no code — the key is present only when there is one
    assert "code" not in res.orchestration["attempts"][1]
