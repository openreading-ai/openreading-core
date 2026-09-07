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
from openreading.types.errors import (
    PlanExhaustedError,
    RetryableError,
    TerminalError,
    UnsupportedFeatureError,
)
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry

CLEAN = "The quick brown fox jumps over the lazy dog, and it does this every single day here. " * 4
GARBLED = "Ã©Ã¨ÃªÃ«Å â€™Ã±Â§Â¶ Ã Ã¢Ã¤ Ãµ Ã¼Ã¿ â‚¬Â£Â¥ Ã˜Ã† Ã‡Ã‰ " * 4


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


# ---- BL-126: honest money — usage.cost_basis reconciles alongside cost_usd --------------------


# ---- BL-134: a rung with known cost but unset basis is folded as contributing nothing -----


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
        ScriptedBackend("reducto", text="unused"),
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
        ScriptedBackend("reducto", text=CLEAN),
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
        ScriptedBackend("reducto", error=TerminalError("boom", backend_code="server")),
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
        ScriptedBackend("reducto", error=TerminalError("boom", backend_code="server")),
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
        ScriptedBackend("reducto", text=CLEAN),
    )
    with pytest.raises(PlanExhaustedError) as ei:
        _run({"version": 1, "strategies": {"s": {"steps": ["pymupdf", "reducto"]}}}, "s", reg)
    # never advanced to reducto — a corrupt doc fails on every backend
    assert [t["category"] for t in ei.value.trail] == ["error(invalid_input)"]


def test_transient_error_advances():
    reg = scripted_registry(
        ScriptedBackend("reducto", error=TerminalError("5xx", backend_code="server")),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )
    res = _run({"version": 1, "strategies": {"s": {"steps": ["reducto", "pymupdf"]}}}, "s", reg)
    assert res.response.backend.id == "pymupdf"
    assert _cats(res) == [("reducto", "error(provider_error)"), ("pymupdf", "succeeded")]


def test_on_error_fail_override_stops_chain():
    reg = scripted_registry(
        ScriptedBackend("reducto", error=TerminalError("5xx", backend_code="server")),
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
        ScriptedBackend("reducto", error=TerminalError("5xx", backend_code="server")),
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
        ScriptedBackend("reducto", required_env=["OPENREADING_TEST_NEVERSET_KEY"], text=CLEAN),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )
    res = _run({"version": 1, "strategies": {"s": {"steps": ["reducto", "pymupdf"]}}}, "s", reg)
    assert res.response.backend.id == "pymupdf"
    assert _cats(res) == [("reducto", "skipped(missing_credentials)"), ("pymupdf", "succeeded")]


# ---- auto leaf + attempted set ----------------------------------------------------------------


# ---- compile / prune pipeline -----------------------------------------------------------------


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
        {"document": {"path": "/d.pdf"}, "backend": {"id": None}}
    )
    # legacy fallback chain: a fails → b wins
    legacy = execute_plan(
        RoutePlan(
            chosen=ScriptedBackend("a", error=err),
            fallbacks=[ScriptedBackend("b", local=True, text=CLEAN)],
        ),
        plain_req,
        broker=EnvCredentialBroker(),
    )
    # equivalent desugared cascade
    reg = scripted_registry(
        ScriptedBackend("a", error=err),
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
        ScriptedBackend("reducto", text=CLEAN),
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
    # ScopeRefused)` clause (execution.md §3) never matched a plain, non-AdapterError exception
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
            required_env=["OPENREADING_TEST_BRANCH_PLAIN_KEY"],
            normalize_error=crash,
        ),
        ScriptedBackend("aws-textract", text=CLEAN),
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
        ScriptedBackend("reducto", text=CLEAN),
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
        ScriptedBackend("reducto", error=TerminalError("boom", backend_code="server")),
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
