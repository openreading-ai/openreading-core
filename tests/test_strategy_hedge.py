"""Milestone 13.2 — hedging + shadow + drain + budget reservation + require (execution.md §3).

Uses the coordinated FakeClock for deterministic timing. `latency_ms` and `start_after` are both
expressed in the SAME unit (ms) so a hedge's delay is comparable to a branch's completion time.
"""

from __future__ import annotations

import base64

import pytest

from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry

CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3


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


def _cats(res):
    return [(a["backend"], a["category"]) for a in res.orchestration["attempts"]]


# ---- deadline_ms propagation (BL-146) ----------------------------------------------------------


@pytest.mark.parametrize(("limit", "expected"), [("1s", 1000), ("0ms", 0)])
def test_leaf_dispatch_threads_remaining_budget_into_run_context(limit, expected):
    # RunContext.deadline_ms is the field an adapter's own code actually reads (e.g.
    # TesseractAdapter.submit()'s subprocess timeout) — a leaf's declared budget.max_duration
    # (tight or an explicit "0ms") must reach it at the strategy-engine leaf-dispatch point, not
    # leave it silently pinned to DEFAULT_DEADLINE_MS. Node-level `budget` (not the deployment-
    # level `limits.max_duration_per_doc`) is used so this exercises only the leaf-dispatch
    # propagation this item fixes, not the separate outer-walk deadline resolution BL-142 already
    # covers.
    backend = ScriptedBackend("pymupdf", local=True, text=CLEAN)
    reg = scripted_registry(backend)
    _run(
        {
            "version": 1,
            "strategies": {"s": {"backend": "pymupdf", "budget": {"max_duration": limit}}},
        },
        reg,
    )
    assert backend.contexts[-1].deadline_ms == expected


# ---- start_after hedging ----------------------------------------------------------------------


def test_hedge_fires_when_primary_slow():
    # primary latency 60ms > hedge start_after 20ms → hedge fires (done at 25ms) and wins
    reg = scripted_registry(
        ScriptedBackend("reducto", text=CLEAN, latency_ms=60),
        ScriptedBackend("aws-textract", text=CLEAN, latency_ms=5),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["reducto", {"backend": "aws-textract", "start_after": "20ms"}],
                    "pick": "fastest",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "aws-textract"


def test_hedge_cancelled_when_primary_wins_fast():
    # primary latency 5ms, hedge start_after 100ms → primary wins before the hedge ever launches
    hedge = ScriptedBackend("aws-textract", text=CLEAN)
    reg = scripted_registry(ScriptedBackend("reducto", text=CLEAN, latency_ms=5), hedge)
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["reducto", {"backend": "aws-textract", "start_after": "100ms"}],
                    "pick": "fastest",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "reducto"
    assert len(hedge.contexts) == 0  # the hedge never submitted (cancelled during start_after)
    assert ("aws-textract", "raced_lost") in _cats(res)


def test_start_after_past_deadline_is_pruned():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", text=CLEAN),
    )
    # a 5s hedge under a 1s node deadline can never launch
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "parallel": ["pymupdf", {"backend": "reducto", "start_after": "5s"}],
                "pick": "best",
                "budget": {"max_cost_usd": 1.0, "max_duration": "1s"},
            }
        },
    }
    res = _run(cfg, reg)
    assert ("reducto", "deadline_pruned") in _cats(res)


# ---- shadow -----------------------------------------------------------------------------------


def test_shadow_runs_but_never_wins():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", text=CLEAN),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["pymupdf", {"backend": "reducto", "shadow": True}],
                    "pick": "fastest",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "pymupdf"  # the shadow never wins
    assert ("reducto", "shadow") in _cats(res)


def test_shadow_faster_still_never_wins():
    # even if the shadow would be fastest, it is excluded from the race
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=30),
        ScriptedBackend("reducto", text=CLEAN, latency_ms=1),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["pymupdf", {"backend": "reducto", "shadow": True}],
                    "pick": "fastest",
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "pymupdf"


# ---- on_win: drain ----------------------------------------------------------------------------


# ---- require ----------------------------------------------------------------------------------


def test_require_one_returns_on_first_success():
    reg = scripted_registry(
        ScriptedBackend("reducto", text=CLEAN, latency_ms=5),
        ScriptedBackend("aws-textract", text=CLEAN, latency_ms=200),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["reducto", "aws-textract"],
                    "pick": "best",
                    "require": 1,
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "reducto"  # picked after the first success, no full wait


# ---- determinism holds with hedging -----------------------------------------------------------


def test_determinism_with_hedge():
    def once():
        reg = scripted_registry(
            ScriptedBackend("reducto", text=CLEAN, latency_ms=60),
            ScriptedBackend("aws-textract", text=CLEAN, latency_ms=5),
        )
        res = _run(
            {
                "version": 1,
                "strategies": {
                    "s": {
                        "parallel": ["reducto", {"backend": "aws-textract", "start_after": "20ms"}],
                        "pick": "fastest",
                        "budget": {"max_cost_usd": 1.0},
                    }
                },
            },
            reg,
        )
        return _cats(res)

    assert once() == once()


# ---- Law 6: a drain outliving the node deadline returns on time, never blocked ----------------


def test_drain_over_deadline_records_loser_without_cost():
    # winner is instant; the drained loser's latency (500ms) is past the 50ms node deadline, so the
    # response returns on time and the loser is recorded (detail: drain_over_deadline) but WITHOUT a
    # fabricated cost — it was never awaited to completion (Law 6, cost estimation removed).
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=1),
        ScriptedBackend("reducto", text=CLEAN, latency_ms=500),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["pymupdf", "reducto"],
                    "pick": "fastest",
                    "on_win": "drain",
                    "budget": {"max_duration": "50ms"},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "pymupdf"
    drained = [
        a
        for a in res.orchestration["attempts"]
        if a.get("detail") == "drain_over_deadline" and a["backend"] == "reducto"
    ]
    assert len(drained) == 1
    assert "cost_usd" not in drained[0] and "cost_basis" not in drained[0]  # no fabricated estimate


def test_webhook_loser_is_marked_acknowledge_and_drop():
    # a cancelled loser whose backend delivers via WEBHOOK is recorded so a late callback is dropped
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=1),
        ScriptedBackend("reducto", text=CLEAN, latency_ms=500, webhook=True),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["pymupdf", "reducto"],
                    "pick": "fastest",  # on_win: cancel (default) → the slow webhook loser is cancelled
                    "budget": {"max_cost_usd": 1.0},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "pymupdf"
    dropped = res.orchestration.get("webhook_dropped", [])
    assert [d["backend"] for d in dropped] == [
        "reducto"
    ]  # T8: late delivery acknowledged + dropped


def test_shadow_over_deadline_is_still_recorded_without_cost():
    # a shadow whose latency outlives the node deadline must still be RECORDED (§4 M3: shadows are
    # always recorded), but WITHOUT a fabricated cost (Law 6) — never silently dropped from the trace.
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN, latency_ms=1),
        ScriptedBackend("reducto", text=CLEAN, latency_ms=500),
    )
    res = _run(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": ["pymupdf", {"backend": "reducto", "shadow": True}],
                    "pick": "fastest",
                    "budget": {"max_duration": "50ms"},
                }
            },
        },
        reg,
    )
    assert res.response.backend.id == "pymupdf"
    shadow = [a for a in res.orchestration["attempts"] if a["category"] == "shadow"]
    assert len(shadow) == 1 and shadow[0]["backend"] == "reducto"
    assert shadow[0]["detail"] == "drain_over_deadline"  # recorded, not dropped
    assert "cost_usd" not in shadow[0] and "cost_basis" not in shadow[0]  # no fabricated estimate
