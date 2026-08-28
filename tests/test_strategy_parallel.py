"""Milestone 13.1 — parallel core (execution.md §3, integration.md §6 T3/T4).

Deterministic offline concurrency via the coordinated FakeClock (harness H1): branches declare a
virtual latency, the clock advances to the earliest wake only when all branches are parked, so a
race resolves reproducibly. Covers fastest, best, fresh-adapter-per-branch, loser cancel,
all-fail composite exhausted, unanimous invalid_input, per-branch resolution faults, and the
H4c determinism golden.
"""

from __future__ import annotations

import base64
import dataclasses

import pytest

from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock, RealClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, engine, run_strategy
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import CostBasis, CostReport, Job
from openreading.types.enums import JobState
from openreading.types.errors import PlanExhaustedError, TerminalError
from openreading.types.request import OpenReadingRequest
from tests.fakes import PollFaultBackend, ScriptedBackend, scripted_registry

CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3
GARBLED = "Ã©Ã¨ÃªÃ«Å â€™Ã±Â§Â¶ Ã Ã¢Ã¤ Ãµ Ã¼Ã¿ " * 4


class _BilledScriptedBackend(ScriptedBackend):
    """A ScriptedBackend whose `report_cost` reports a real `CostBasis.BILLED` basis (BL-126). The
    base `ScriptedBackend.report_cost` is hardcoded to `infra_only(...)` regardless of `cost_usd`,
    which can never exercise "a billed branch contributed" — this subclass makes that case
    reproducible without touching the shared fixture every other test relies on."""

    def report_cost(self, job: Job) -> CostReport:
        return CostReport(
            native_unit="page",
            native_quantity=1.0,
            cost_usd=self._cost_usd,
            basis=CostBasis.BILLED,
            billing_target="caller_account",
        )


def _req(compliance: dict | None = None):
    payload = {
        "document": {
            "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            "mime_type": "application/pdf",
        },
        "backend": {"id": "strategy:s"},
    }
    if compliance:
        payload["compliance"] = compliance
    return OpenReadingRequest.model_validate(payload)


def _run(cfg, reg, req=None):
    r = req or _req()
    compiled = compile_strategy(r, "s", StrategyConfig.model_validate(cfg), reg, RouterConfig())
    return run_strategy(compiled, r, registry=reg, broker=EnvCredentialBroker(), clock=FakeClock())


def _cats(res):
    return [(a["backend"], a["category"]) for a in res.orchestration["attempts"]]


# ---- pick: fastest ----------------------------------------------------------------------------


def test_race_picks_lowest_latency():
    reg = scripted_registry(
        ScriptedBackend("docling", local=True, text=CLEAN, latency_ms=20),
        ScriptedBackend("tesseract", local=True, text=CLEAN, latency_ms=5),  # faster
    )
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"parallel": ["docling", "tesseract"], "pick": "fastest"}},
        },
        reg,
    )
    assert res.response.backend.id == "tesseract"
    assert _cats(res) == [("docling", "raced_lost"), ("tesseract", "succeeded")]


def test_race_ties_break_by_branch_index():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=10),
        ScriptedBackend("tesseract", local=True, text=CLEAN, latency_ms=10),  # same latency
    )
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"parallel": ["pymupdf", "tesseract"], "pick": "fastest"}},
        },
        reg,
    )
    assert res.response.backend.id == "pymupdf"  # branch 0 wins the tie


def test_race_failed_fast_branch_does_not_win():
    reg = scripted_registry(
        ScriptedBackend(
            "reducto",
            cost_low=0.01,
            latency_ms=5,
            error=TerminalError("5xx", backend_code="server"),
        ),
        ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=20),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["reducto", "pymupdf"],
                    "pick": "fastest",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "pymupdf"  # a fast failure loses the race
    assert ("reducto", "error(provider_error)") in _cats(res)


# ---- pick: best -------------------------------------------------------------------------------


def test_best_picks_higher_quality():
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=GARBLED),  # low quality
        ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN),  # high quality
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["reducto", "aws-textract"],
                    "pick": "best",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "aws-textract"
    assert ("reducto", "judged_lost") in _cats(res)


def test_best_tie_breaks_cheaper_backend():
    reg = scripted_registry(
        ScriptedBackend("aws-textract", cost_low=0.10, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),  # same quality, cheaper
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["aws-textract", "reducto"],
                    "pick": "best",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "reducto"


# ---- fresh adapter, money, composite failure --------------------------------------------------


def test_each_branch_runs_its_own_backend():
    # pick: best runs both branches to completion — each drives its OWN distinct adapter exactly
    # once (T4: sibling branches have distinct backends, so no instance is shared concurrently).
    a = ScriptedBackend("reducto", cost_low=0.01, text=CLEAN)
    b = ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN)
    reg = scripted_registry(a, b)
    _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["reducto", "aws-textract"],
                    "pick": "best",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert len(a.contexts) == 1 and len(b.contexts) == 1


def test_race_loser_cancelled_before_running():
    winner = ScriptedBackend("tesseract", local=True, text=CLEAN, latency_ms=5)
    loser = ScriptedBackend("docling", local=True, text=CLEAN, latency_ms=50)
    reg = scripted_registry(loser, winner)
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"parallel": ["docling", "tesseract"], "pick": "fastest"}},
        },
        reg,
    )
    assert res.response.backend.id == "tesseract"
    assert len(loser.contexts) == 0  # cancelled during its latency sleep, before submit
    assert ("docling", "raced_lost") in _cats(res)


# ---- on_win: cancel reaches the backend, not just the task (execution.md §3.3) -----------------


def _race_with_poll_loser(reg, *, on_win: str | None = None):
    node: dict = {
        "parallel": ["pymupdf", "reducto"],
        "pick": "fastest",
        "budget": {"max_cost_usd": 1.0},
    }
    if on_win is not None:
        node["on_win"] = on_win
    return _run({"version": 1, "strategies": {"s": node}}, reg)


def test_race_cancels_a_losers_live_backend_job():
    # Fault: the POLL branch's status call 500s AFTER the backend accepted the job, so the branch
    # loses the race with its job still RUNNING at the vendor. `on_win: cancel` (the default) owes
    # that job an adapter.cancel() — task cancellation alone never reaches the backend, and an
    # uncancelled job keeps running (and billing) long after the winner was picked.
    loser = PollFaultBackend("reducto")
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=1))
    reg.register(loser)
    res = _race_with_poll_loser(reg)
    assert res.response.backend.id == "pymupdf"
    assert ("reducto", "error(provider_error)") in _cats(res)
    assert len(loser.cancelled) == 1  # exactly one cancel, for the job it submitted
    assert loser.cancelled[0] is loser.submitted[0]
    assert loser.submitted[0].state is JobState.CANCELLED  # POLL loser stops being polled


def test_on_win_drain_leaves_a_losers_job_alone():
    # The contrast that pins the gate: under `on_win: drain` losers are allowed to finish (and are
    # billed), so the engine must never cancel their backend jobs.
    loser = PollFaultBackend("reducto")
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=1))
    reg.register(loser)
    res = _race_with_poll_loser(reg, on_win="drain")
    assert res.response.backend.id == "pymupdf"
    assert loser.submitted and loser.cancelled == []


def test_multiple_losers_are_cancelled_concurrently_not_serially():
    # BL-164 review (a reviewer, High): adapter.cancel() now issues a real, synchronous vendor HTTP call
    # for four real adapters (previously an instant in-memory no-op for all thirteen) — a plain,
    # unwrapped call per loser inside `_eval_parallel` (an async function) would block THIS
    # process's entire event loop for the sum of every loser's round trip, serially, stalling
    # every other concurrent request on the same worker. Simulates that round trip with a real
    # (not virtual-clock) `time.sleep` in two losers' `cancel()` and asserts the wall-clock cost of
    # cancelling BOTH is close to ONE sleep, not the sum of two — proving they're dispatched
    # concurrently (via asyncio.to_thread + gather), not one after another.
    import time

    SLEEP_S = 0.3
    loser_a = PollFaultBackend("reducto", cancel_sleep_s=SLEEP_S)
    loser_b = PollFaultBackend("azure-document-intelligence", cancel_sleep_s=SLEEP_S)
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=1))
    reg.register(loser_a)
    reg.register(loser_b)
    node = {
        "parallel": ["pymupdf", "reducto", "azure-document-intelligence"],
        "pick": "fastest",
        "budget": {"max_cost_usd": 1.0},
    }
    start = time.monotonic()
    res = _run({"version": 1, "strategies": {"s": node}}, reg)
    elapsed = time.monotonic() - start
    assert res.response.backend.id == "pymupdf"
    assert len(loser_a.cancelled) == 1 and len(loser_b.cancelled) == 1  # both genuinely cancelled
    # Serial dispatch costs >= 2 * SLEEP_S (0.60s) plus whatever fixed engine overhead this test
    # also pays; concurrent dispatch costs ~1 * SLEEP_S (0.30s) plus that same fixed overhead.
    # The threshold sits well above the concurrent case (including generous jitter headroom) and
    # well below the serial one, so it fails hard under either dispatch shape without being flaky.
    assert elapsed < SLEEP_S * 1.8, (
        f"cancelling two losers took {elapsed:.3f}s — looks serial, not concurrent "
        f"(2x{SLEEP_S}={2 * SLEEP_S:.2f}s expected if serial, ~1x{SLEEP_S}={SLEEP_S:.2f}s "
        "expected if concurrent)"
    )


def test_cancel_dispatch_never_blocks_the_response_past_the_node_deadline():
    # BL-164 review round 2 (High): concurrent dispatch alone doesn't cap the TOTAL wait —
    # a single slow vendor cancel could still extend the response well past the node's own
    # configured deadline, breaking the exact Law 6 promise `_drain` already keeps for a drained
    # loser ("the response never blocks past the node deadline"). Reproduces the reviewer's own repro
    # shape exactly: a real (not FakeClock) clock, a tight budget.max_duration, and a loser whose
    # cancel() sleeps far longer than that budget — asserts the response returns close to the
    # budget, not close to the sleep.
    import time

    SLEEP_S = 1.0
    BUDGET_S = 0.2
    loser = PollFaultBackend("reducto", cancel_sleep_s=SLEEP_S)
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=1))
    reg.register(loser)
    node = {
        "parallel": ["pymupdf", "reducto"],
        "pick": "fastest",
        "budget": {"max_duration": "200ms"},
    }
    req = _req()
    compiled = compile_strategy(
        req,
        "s",
        StrategyConfig.model_validate({"version": 1, "strategies": {"s": node}}),
        reg,
        RouterConfig(),
    )
    start = time.monotonic()
    res = run_strategy(compiled, req, registry=reg, broker=EnvCredentialBroker(), clock=RealClock())
    elapsed = time.monotonic() - start
    assert res.response.backend.id == "pymupdf"
    # The attempt was genuinely dispatched (not skipped) — but deliberately NOT waited out: the
    # response returns before cancel() itself finishes sleeping, so `cancelled` (appended only
    # after the sleep) is correctly still empty here; that's the whole point of "abandoned, not
    # retried" — only `cancel_started` (appended before the sleep) proves dispatch happened.
    assert len(loser.cancel_started) == 1
    # Without the bound, this would take ~SLEEP_S (1.0s). With it, the response returns close to
    # BUDGET_S (0.2s) — well under half of SLEEP_S either way, so the assertion can't pass by
    # accident of scheduling jitter.
    assert elapsed < SLEEP_S / 2, (
        f"response took {elapsed:.3f}s — looks like it waited out the slow cancel "
        f"({SLEEP_S:.2f}s) instead of respecting the node's own {BUDGET_S:.2f}s deadline"
    )


def test_money_sums_all_billed_branches():
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN, cost_usd=0.05),
        ScriptedBackend("aws-textract", cost_low=0.01, text=CLEAN, cost_usd=0.09),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["reducto", "aws-textract"],
                    "pick": "best",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    # both branches completed (best waits for all) → usage.cost_usd sums winner + loser (T9)
    assert res.response.usage.cost_usd == pytest.approx(0.14)


def test_composite_winner_keeps_its_own_cost_plus_siblings():
    # the winner is a COMPOSITE branch (nested cascade → reducto, own cost 0.10); a leaf sibling
    # loser bills 0.08. The total must be own + sibling = 0.18, not just the sibling's.
    reg = scripted_registry(
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN, cost_usd=0.10),
        ScriptedBackend("aws-textract", cost_low=0.01, text=GARBLED, cost_usd=0.08),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": [
                        {"steps": ["reducto"]},
                        "aws-textract",
                    ],  # composite branch wins on quality
                    "pick": "best",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "reducto"  # the CLEAN composite branch wins
    assert res.response.usage.cost_usd == pytest.approx(0.18)  # 0.10 own + 0.08 sibling loser


# ---- BL-126: honest money — usage.cost_basis reconciles alongside cost_usd --------------------


def test_money_reconciles_cost_basis_when_a_billed_loser_completes():
    # the winner is free/local; a billed loser still completes and is billed (T9). Before this fix,
    # _resolve_parallel folded the loser's cost_usd into the total but never touched cost_basis, so
    # the response kept asserting the free winner's own "infra_only" — a real vendor charge
    # silently reported as free.
    winner = ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=1)
    loser = _BilledScriptedBackend("reducto", cost_usd=0.09, text=GARBLED, latency_ms=5)
    reg = scripted_registry(winner, loser)
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["pymupdf", "reducto"],
                    "pick": "best",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "pymupdf"  # the CLEAN branch wins on quality
    assert res.response.usage.cost_usd == pytest.approx(0.09)
    assert res.response.usage.cost_basis == "billed"


def test_merge_reconciles_cost_basis_across_base_and_source_branches():
    # pick: merge folds every branch's cost through the identical T9 loop pick: best uses — the
    # merge_base/merge_source categories are cosmetic to cost accounting. A billed source branch's
    # basis must not be dropped just because it lost the merge_base slot to the free branch.
    base = ScriptedBackend("pymupdf", local=True, text=CLEAN)  # infra_only, wins merge_base
    source = _BilledScriptedBackend("reducto", cost_usd=0.07, text=GARBLED)  # merge_source, billed
    reg = scripted_registry(base, source)
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["pymupdf", "reducto"],
                    "pick": "merge",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.usage.cost_usd == pytest.approx(0.07)
    assert res.response.usage.cost_basis == "billed"


# ---- BL-134/Alex: a composite branch's cost never reaches any fold at all ---------------------


def test_composite_loser_cost_is_not_dropped():
    # _resolve_parallel's per-branch loop used to `continue` on every composite branch BEFORE any
    # cost/basis folding ran — dropping a losing composite's real, already-computed cost entirely.
    # Same-cost, same-content control isolating "compositeness" as the only variable (mirrors
    # test_composite_winner_keeps_its_own_cost_plus_siblings, but the composite branch loses
    # instead of wins): the composite loser's own 0.08 must still reach usage.cost_usd.
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=1),
        ScriptedBackend("aws-textract", cost_low=0.01, text=GARBLED, cost_usd=0.08),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": [
                        "pymupdf",
                        {"steps": ["aws-textract"]},  # composite branch, loses on quality
                    ],
                    "pick": "best",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "pymupdf"  # the CLEAN leaf wins
    assert res.response.usage.cost_usd == pytest.approx(0.08)  # composite loser's cost, not dropped
    assert res.response.usage.cost_basis not in (None, "infra_only")


def test_composite_shadow_cost_is_not_dropped():
    # the identical drop, on a composite branch that SHADOWS and completes successfully within the
    # node deadline rather than losing a comparison (mirrors test_shadow_runs_but_never_wins,
    # wrapped as a composite branch — the officially documented use:-as-a-parallel-branch idiom,
    # the openreading.strategies.presets docstring's own `audited` strategy, generalizes to any composite shape).
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN, cost_usd=0.05),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["pymupdf", {"steps": ["reducto"], "shadow": True}],
                    "pick": "fastest",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "pymupdf"  # the shadow never wins
    assert res.response.usage.cost_usd == pytest.approx(0.05)  # the shadow's cost, not dropped
    assert res.response.usage.cost_basis not in (None, "infra_only")


def test_composite_winner_raising_report_cost_never_looks_infra_only():
    # BL-134, the composite-winner site specifically (:1135 pre-fix): its own basis fold used
    # to pass resp.usage.cost_basis straight through with no coalescing. Here the composite winner's
    # own internal leaf's report_cost() raises AFTER normalize() already set cost_usd (router/
    # cost.py's own "an adapter meters a channel itself" pattern) — the nested response ends up with
    # a real cost and an unset basis. The sibling loser's own genuinely-free-shaped "infra_only" tag
    # would otherwise outrank a bare coalesced "unknown" by priority (_COST_BASIS_PRIORITY ranks
    # unknown below infra_only) — _set_total_cost's own hardening must still keep the final response
    # honest.
    reg = scripted_registry(
        ScriptedBackend(
            "reducto",
            cost_low=0.01,
            text=CLEAN,
            cost_usd=0.10,
            report_cost_error=RuntimeError("meter exploded"),
        ),
        ScriptedBackend("aws-textract", cost_low=0.01, text=GARBLED, cost_usd=0.08),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": [{"steps": ["reducto"]}, "aws-textract"],
                    "pick": "best",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "reducto"  # the CLEAN composite branch wins
    assert res.response.usage.cost_usd == pytest.approx(0.18)  # 0.10 own + 0.08 sibling loser
    assert res.response.usage.cost_basis not in (None, "infra_only")


def test_all_branches_fail_composite_exhausted():
    reg = scripted_registry(
        ScriptedBackend(
            "reducto", cost_low=0.01, error=TerminalError("5xx", backend_code="server")
        ),
        ScriptedBackend(
            "aws-textract", cost_low=0.01, error=TerminalError("5xx", backend_code="server")
        ),
    )
    with pytest.raises(PlanExhaustedError):
        _run(
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
            reg,
        )


def test_unanimous_invalid_input_propagates():
    # both branches say invalid_input → the parallel node resolves invalid_input; inside a cascade
    # invalid_input is fail-by-default → the walk stops and never reaches pymupdf.
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [
                    {
                        "parallel": ["reducto", "aws-textract"],
                        "pick": "fastest",
                        "budget": {"max_cost_usd": 1.0},
                    },
                    "pymupdf",
                ]
            }
        },
    }
    reg2 = scripted_registry(
        ScriptedBackend(
            "reducto",
            cost_low=0.01,
            error=TerminalError("corrupt", backend_code="corrupt_document"),
        ),
        ScriptedBackend(
            "aws-textract",
            cost_low=0.01,
            error=TerminalError("corrupt", backend_code="corrupt_document"),
        ),
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
    )
    with pytest.raises(
        PlanExhaustedError
    ):  # invalid_input is fail-by-default → never reaches pymupdf
        _run(cfg, reg2)


# ---- H4c determinism golden -------------------------------------------------------------------


def test_determinism_golden():
    def once():
        reg = scripted_registry(
            ScriptedBackend("docling", local=True, text=CLEAN, latency_ms=20),
            ScriptedBackend("tesseract", local=True, text=CLEAN, latency_ms=5),
        )
        res = _run(
            {
                "version": 1,
                "strategies": {"s": {"parallel": ["docling", "tesseract"], "pick": "fastest"}},
            },
            reg,
        )
        return _cats(res), res.orchestration["decisions"]

    assert once() == once()  # same tree + doc under FakeClock → identical attempts + decisions


# ---- P2: gates on parallel steps (§6.3, harness §15 T5) ---------------------------------------
#
# A cascade step whose node is a `parallel` may carry `escalate_if`; the engine evaluates it on
# the comparison/race WINNER with leaf-step semantics (fire -> retain best-so-far + advance;
# else accept). This is the desugar target of Plain `compare:`+`then:`, and it makes the
# cookbook's "wrap the race in a gated cascade step" idiom real. Ungated composites are
# unchanged (the no-regression half is the rest of this file staying green).


def _find_attempt(res, *, category):
    for a in res.orchestration["attempts"]:
        if a["category"] == category:
            return a
    return None


def _compare_then(gate, then="reducto", pick="best", budget=1.0):
    body = {"parallel": ["docling", "aws-textract"], "pick": pick, "escalate_if": gate}
    if pick == "best":
        body["require"] = "all"
    return {
        "version": 1,
        "strategies": {"s": {"budget": {"max_cost_usd": budget}, "steps": [body, then]}},
    }


def test_gated_parallel_step_winner_passes_is_accepted():
    # both branches clean -> the winner passes the garbled gate -> accepted; the `then` rung
    # (reducto) never runs.
    reg = scripted_registry(
        ScriptedBackend("docling", cost_low=0.02, cost_usd=0.02, text=CLEAN),
        ScriptedBackend("aws-textract", cost_low=0.02, cost_usd=0.02, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.05, cost_usd=0.05, text=CLEAN),
    )
    res = _run(_compare_then({"garbled": True}), reg)
    backends = [b for b, _ in _cats(res)]
    assert "reducto" not in backends  # accepted before the escape rung
    winner = _find_attempt(res, category="succeeded")
    assert winner is not None
    assert any(g["predicate"] == "garbled" and not g["fired"] for g in winner["gates"])


def test_gated_parallel_step_winner_fails_escalates_to_then():
    # both branches garbled -> the winner trips the gate -> retained, `then` (reducto) runs & wins.
    reg = scripted_registry(
        ScriptedBackend("docling", cost_low=0.02, cost_usd=0.02, text=GARBLED),
        ScriptedBackend("aws-textract", cost_low=0.02, cost_usd=0.02, text=GARBLED),
        ScriptedBackend("reducto", cost_low=0.05, cost_usd=0.05, text=CLEAN),
    )
    res = _run(_compare_then({"garbled": True}), reg)
    assert res.response.backend.id == "reducto"
    escalated = _find_attempt(res, category="quality_escalated")
    assert escalated is not None and escalated["backend"] in ("docling", "aws-textract")
    assert any(g["predicate"] == "garbled" and g["fired"] for g in escalated["gates"])


def test_gate_on_pick_fastest_step_is_the_cookbook_idiom():
    # the "wrap the race in a gated cascade step" idiom: per-branch gates are not evaluated in a
    # race, but a gate on the STEP is evaluated on the race winner.
    reg = scripted_registry(
        ScriptedBackend("docling", local=True, text=GARBLED, latency_ms=5),
        ScriptedBackend("aws-textract", local=True, text=GARBLED, latency_ms=20),
        ScriptedBackend("reducto", cost_low=0.05, cost_usd=0.05, text=CLEAN),
    )
    res = _run(_compare_then({"garbled": True}, pick="fastest"), reg)
    assert res.response.backend.id == "reducto"
    escalated = _find_attempt(res, category="quality_escalated")
    assert escalated is not None and escalated["backend"] == "docling"  # the faster branch won


def test_gated_final_parallel_step_is_deficient_keep_best():
    # a gated parallel step in FINAL position: the winner trips the gate, nothing to escalate to,
    # so keep-best returns it as Deficient with an honest quality warning (advanced shape).
    reg = scripted_registry(
        ScriptedBackend("docling", cost_low=0.02, cost_usd=0.02, text=GARBLED),
        ScriptedBackend("aws-textract", cost_low=0.02, cost_usd=0.02, text=GARBLED),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "budget": {"max_cost_usd": 1.0},
                "steps": [
                    {
                        "parallel": ["docling", "aws-textract"],
                        "pick": "best",
                        "escalate_if": {"garbled": True},
                    }
                ],
            }
        },
    }
    res = _run(cfg, reg)
    assert res.response.backend.id in ("docling", "aws-textract")
    assert res.orchestration["outcome"] == "degraded"  # keep-best returned a Deficient result
    escalated = _find_attempt(res, category="quality_escalated")
    assert escalated is not None


def test_ungated_parallel_step_does_not_gate_no_regression():
    # no escalate_if on the parallel step -> the winner is accepted even when garbled; the `then`
    # rung never runs. Proves P2 changes behavior ONLY when a step gate is present.
    reg = scripted_registry(
        ScriptedBackend("docling", cost_low=0.02, cost_usd=0.02, text=GARBLED),
        ScriptedBackend("aws-textract", cost_low=0.02, cost_usd=0.02, text=GARBLED),
        ScriptedBackend("reducto", cost_low=0.05, cost_usd=0.05, text=CLEAN),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "budget": {"max_cost_usd": 1.0},
                "steps": [{"parallel": ["docling", "aws-textract"], "pick": "best"}, "reducto"],
            }
        },
    }
    res = _run(cfg, reg)
    assert "reducto" not in [b for b, _ in _cats(res)]  # accepted, not escalated
    assert _find_attempt(res, category="quality_escalated") is None


def test_gated_parallel_step_with_composite_winner_still_gates():
    # a parallel branch that is itself a composite (nested cascade) can win — here the other
    # branch errors, so the composite is the sole success. The step gate is still evaluated on its
    # response (garbled -> escalate), though a composite winner has no single leaf attempt to
    # annotate (the `winner is None` path).
    reg = scripted_registry(
        ScriptedBackend("docling", cost_low=0.02, cost_usd=0.02, text=GARBLED),
        ScriptedBackend(
            "aws-textract", cost_low=0.02, error=TerminalError("5xx", backend_code="server")
        ),
        ScriptedBackend("reducto", cost_low=0.05, cost_usd=0.05, text=CLEAN),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "budget": {"max_cost_usd": 1.0},
                "steps": [
                    {
                        "parallel": [{"steps": ["docling"]}, "aws-textract"],
                        "pick": "best",
                        "escalate_if": {"garbled": True},
                    },
                    "reducto",
                ],
            }
        },
    }
    res = _run(cfg, reg)
    assert res.response.backend.id == "reducto"  # composite winner tripped the gate → escalated


class _StubPort:
    """Stand-in for the `DeciderPort` / `JudgePort` seams: the field-coverage guard below only needs
    a non-default object identity in those two slots, so neither method is ever invoked."""

    def decide(self, dp):
        raise AssertionError("stub port invoked")

    def compare(self, a, b, intent):
        raise AssertionError("stub port invoked")


def test_child_ctx_carries_every_walk_field_overriding_the_deadline_only():
    # A composite cascade step walks a CHILD context carrying the step's clamped deadline. That copy
    # was hand-written field by field and silently dropped keep_candidates / candidates /
    # plain_sourced — a cascade-wrapped race retained zero candidates. Guard the whole struct, so a
    # field added later cannot go missing the same way. Every value below differs in identity from
    # its field default, so a dropped field shows up as a mismatch.
    from openreading.strategies.engine import _child_ctx, _WalkCtx
    from openreading.strategies.trace import Trace

    ctx = _WalkCtx(
        req=_req(),
        registry=scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN)),
        broker=EnvCredentialBroker(),
        clock=FakeClock(),
        trace=Trace(strategy="s", config_hash="h"),
        trees={},
        eligible=["pymupdf"],
        deadline_ms=9000.0,
        decider_llm=_StubPort(),
        judge_llm=_StubPort(),
        config_hash="h",
        strategy_name="s",
        plain_sourced=True,
        router_config=RouterConfig(),
        replay={},
        keep_candidates=True,
        candidates=[{"backend": "pymupdf"}],
    )
    child = _child_ctx(ctx, 500.0)

    assert child.deadline_ms == 500.0
    assert [
        f.name
        for f in dataclasses.fields(_WalkCtx)
        if f.name != "deadline_ms" and getattr(child, f.name) is not getattr(ctx, f.name)
    ] == []


def test_gated_parallel_after_an_escalated_leaf_step():
    # the gated parallel is NOT the first step: a leaf escalates first, so the winner-attempt scan
    # must skip that earlier leaf's attempt (a different path) and still bind the parallel winner.
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=GARBLED),  # step 0 escalates
        ScriptedBackend("docling", cost_low=0.02, cost_usd=0.02, text=CLEAN),
        ScriptedBackend("aws-textract", cost_low=0.02, cost_usd=0.02, text=CLEAN),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "budget": {"max_cost_usd": 1.0},
                "steps": [
                    {"backend": "pymupdf", "escalate_if": {"garbled": True}},
                    {
                        "parallel": ["docling", "aws-textract"],
                        "pick": "best",
                        "escalate_if": {"garbled": True},
                    },
                ],
            }
        },
    }
    res = _run(cfg, reg)
    assert res.response.backend.id in ("docling", "aws-textract")  # clean winner passes its gate
    winner = _find_attempt(res, category="succeeded")
    assert winner is not None and any(g["predicate"] == "garbled" for g in winner["gates"])


# ---- P7: disagreement_over signal (§11 phase 2) -----------------------------------------------
#
# On a pick:best parallel step, the engine computes 1 - content_overlap over the finished branches
# (comparison/deltas.py) and exposes it to the wrapping cascade step's gate as `disagreement_over`.
# This is what Plain `compare` + `then`'s `disagree` compiles to.

_DISJOINT_A = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
_DISJOINT_B = "one two three four five six seven eight nine ten"


def _compare_then_disagree(then="reducto", gate=None, budget=1.0):
    body = {
        "parallel": ["docling", "aws-textract"],
        "pick": "best",
        "require": "all",
        "escalate_if": gate or {"disagreement_over": 0.3},
    }
    return {
        "version": 1,
        "strategies": {"s": {"budget": {"max_cost_usd": budget}, "steps": [body, then]}},
    }


def test_disagreement_over_fires_when_branches_disagree():
    reg = scripted_registry(
        ScriptedBackend("docling", cost_low=0.01, cost_usd=0.01, text=_DISJOINT_A),
        ScriptedBackend("aws-textract", cost_low=0.01, cost_usd=0.01, text=_DISJOINT_B),
        ScriptedBackend("reducto", cost_low=0.05, cost_usd=0.05, text=CLEAN),
    )
    res = _run(_compare_then_disagree(), reg)
    assert res.response.backend.id == "reducto"  # disagreement escalated to the then rung
    escalated = _find_attempt(res, category="quality_escalated")
    assert escalated is not None
    assert any(g["predicate"] == "disagreement_over" and g["fired"] for g in escalated["gates"])


def test_disagreement_over_does_not_fire_when_branches_agree():
    reg = scripted_registry(
        ScriptedBackend("docling", cost_low=0.01, cost_usd=0.01, text=CLEAN),
        ScriptedBackend("aws-textract", cost_low=0.01, cost_usd=0.01, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.05, cost_usd=0.05, text=CLEAN),
    )
    res = _run(_compare_then_disagree(), reg)
    assert "reducto" not in [b for b, _ in _cats(res)]  # identical output → winner accepted


def test_disagreement_telemetry_recorded_on_pick_best_winner():
    # unconditional: a pick:best node records the disagreement even with no gate referencing it.
    reg = scripted_registry(
        ScriptedBackend("docling", cost_low=0.01, cost_usd=0.01, text=_DISJOINT_A),
        ScriptedBackend("aws-textract", cost_low=0.01, cost_usd=0.01, text=_DISJOINT_B),
    )
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "parallel": ["docling", "aws-textract"],
                "pick": "best",
                "budget": {"max_cost_usd": 1.0},
            }
        },
    }
    res = _run(cfg, reg)
    winner = _find_attempt(res, category="succeeded")
    assert winner is not None and winner.get("disagreement") is not None
    assert winner["disagreement"] > 0.3  # disjoint vocabularies → high disagreement


# ---- branch resolution faults (execution.md §3.1) ---------------------------------------------
#
# Before a branch can race it must resolve its configured name to a live adapter it is allowed to
# call. Three ways that fails, none of which is exotic — execution.md's own examples mix local and
# hosted backends in one `parallel:` block: the name resolves to nothing (an `auto` branch with no
# untried eligible backend left after routing), the name has no adapter in this process, or the
# adapter is there but its credentials are not. Each must retire its own branch with an honest
# status and leave the race to resolve among the survivors — never take the node down with it.


def _branch_outcomes(monkeypatch) -> dict[int, engine._BranchOutcome]:
    """Every `_BranchOutcome` the parallel node produced, by branch index — the resolver's own
    return value, not just its projection into the trace."""
    seen: dict[int, engine._BranchOutcome] = {}
    real = engine._run_branch

    async def spy(*args, **kwargs):
        outcome = await real(*args, **kwargs)
        seen[outcome.index] = outcome
        return outcome

    monkeypatch.setattr(engine, "_run_branch", spy)
    return seen


def test_branch_auto_with_nothing_left_to_resolve_errors_exhausted(monkeypatch):
    # `require_local` drops the hosted backend at routing, so the sibling `auto` branch has no
    # untried eligible backend once branch 0 claims the only local one (T12, walk-wide attempted).
    outcomes = _branch_outcomes(monkeypatch)
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),  # non-local → routed out
    )
    res = _run(
        {"version": 1, "strategies": {"s": {"parallel": ["pymupdf", "auto"], "pick": "fastest"}}},
        reg,
        req=_req({"require_local": True}),
    )
    assert outcomes[1].status == "error"
    assert outcomes[1].error_class == "exhausted"
    assert outcomes[1].backend == ""  # nothing resolved, so nothing to name
    assert res.response.backend.id == "pymupdf"  # the race still has a real winner
    assert ("auto", "error(exhausted)") in _cats(res)


def test_branch_backend_absent_from_registry_errors_provider_error(monkeypatch):
    # `reducto` is named by the strategy but registered nowhere in this process: the router never
    # saw it, so pruning kept the branch, and the resolver is the first thing to notice.
    outcomes = _branch_outcomes(monkeypatch)
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=5))
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"parallel": ["reducto", "pymupdf"], "pick": "fastest"}},
        },
        reg,
    )
    assert outcomes[0].status == "error"
    assert outcomes[0].error_class == "provider_error"
    assert outcomes[0].backend == "reducto"
    assert res.response.backend.id == "pymupdf"
    assert ("reducto", "error(provider_error)") in _cats(res)


def test_branch_missing_credentials_skips_and_the_race_still_resolves(monkeypatch):
    # a hosted branch whose key is absent is SKIPPED, not failed — it never reached the provider,
    # so it must not colour the composite error class (execution.md §3.2 / spec §5.1).
    outcomes = _branch_outcomes(monkeypatch)
    reg = scripted_registry(
        ScriptedBackend(
            "reducto", cost_low=0.01, required_env=["OPENREADING_TEST_NEVERSET_KEY"], text=CLEAN
        ),
        ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=20),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {"s": {"parallel": ["reducto", "pymupdf"], "pick": "fastest"}},
        },
        reg,
    )
    assert outcomes[0].status == "skip"
    assert outcomes[0].error_class is None
    assert outcomes[0].backend == "reducto"
    assert res.response.backend.id == "pymupdf"
    assert ("reducto", "skipped(missing_credentials)") in _cats(res)


def test_every_branch_unresolvable_is_composite_exhausted(monkeypatch):
    # no survivor to fall back on: both branches die at resolution, so the node resolves
    # `exhausted` (Law 5) rather than hanging on a race that can never be won.
    outcomes = _branch_outcomes(monkeypatch)
    reg = scripted_registry(
        ScriptedBackend(
            "reducto", cost_low=0.01, required_env=["OPENREADING_TEST_NEVERSET_KEY"], text=CLEAN
        )
    )
    with pytest.raises(PlanExhaustedError):
        _run(
            {
                "version": 1,
                "strategies": {"s": {"parallel": ["reducto", "aws-textract"], "pick": "fastest"}},
            },
            reg,
        )
    assert outcomes[0].status == "skip"
    assert outcomes[1].status == "error" and outcomes[1].error_class == "provider_error"
