"""Milestone 12.2 — policy:/limits: blocks (spec §1.3, §6.4).

The file `policy:` block unions into the request's effective compliance (most-restrictive-wins,
constraints only add), feeding BOTH the router prune and the route compliance facts; `limits:`
wraps every strategy-engaged run as the outermost budget but never touches direct-named requests.

`openreading.config.apply` runs that union once, before dispatch, on every path, so the helpers
below call it exactly where `openreading.api` does and then compile the request it produced.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import pytest

from openreading import api
from openreading.config import apply as apply_config
from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ComplianceRefused
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry

CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3


def _req(compliance=None):
    body = {
        "document": {
            "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            "mime_type": "application/pdf",
        },
        "backend": {"id": "strategy:s"},
    }
    if compliance:
        body["compliance"] = compliance
    return OpenReadingRequest.model_validate(body)


def _applied(cfg, req, base=None):
    """The request and RouterConfig a surface hands to `compile_strategy`: the file `policy:`
    block already unioned in, exactly as `openreading.api` does before it dispatches."""
    return apply_config(req, cfg.get("policy"), base or RouterConfig())


def _compile(cfg, name, reg, req, base=None):
    req, router_config = _applied(cfg, req, base)
    return compile_strategy(req, name, StrategyConfig.model_validate(cfg), reg, router_config)


def _run(cfg, name, reg, req):
    req, _ = _applied(cfg, req)
    compiled = _compile(cfg, name, reg, req)
    return run_strategy(
        compiled, req, registry=reg, broker=EnvCredentialBroker(), clock=FakeClock()
    )


# ---- policy union -----------------------------------------------------------------------------


def test_file_policy_unions_into_prune():
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),  # non-local
    )
    cfg = {
        "version": 1,
        "policy": {"require_local": True},
        "strategies": {"s": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"}},
    }
    compiled = _compile(cfg, "s", reg, _req())
    assert compiled.effective_compliance["require_local"] is True
    assert "reducto" in {d.backend for d in compiled.dropped}  # pruned by the file policy


def test_file_policy_feeds_route_compliance_facts():
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN))
    cfg = {
        "version": 1,
        "policy": {"require_local": True},
        "strategies": {
            "s": {
                "route": {
                    "rules": [{"when": {"compliance": {"require_local": True}}, "use": "pymupdf"}],
                    "default": "pymupdf",
                }
            }
        },
    }
    res = _run(cfg, "s", reg, _req())  # request itself sets no compliance
    assert res.orchestration["decisions"][0]["chosen"] == 0  # matched via the FILE policy


def test_most_restrictive_wins_union():
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN))
    cfg = {"version": 1, "policy": {"require_local": True}, "strategies": {"s": ["pymupdf"]}}
    compiled = _compile(cfg, "s", reg, _req(compliance={"no_train_on_data": True}))
    # both the request's no_train and the file's require_local are present (constraints only add)
    assert compiled.effective_compliance["no_train_on_data"] is True
    assert compiled.effective_compliance["require_local"] is True


def test_file_policy_never_widens():
    # a file policy can only tighten: request require_local stays True even if the file omits it
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN))
    cfg = {"version": 1, "policy": {"no_train_on_data": True}, "strategies": {"s": ["pymupdf"]}}
    compiled = _compile(cfg, "s", reg, _req(compliance={"require_local": True}))
    assert compiled.effective_compliance["require_local"] is True


def test_allow_unverified_compliance_from_file_policy_readmits():
    # an unverified-train backend under no_train is dropped — unless the file allows unverified
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, trains="unverified", text=CLEAN),
    )
    strict = {
        "version": 1,
        "strategies": {"s": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"}},
    }
    dropped_strict = {
        d.backend
        for d in _compile(strict, "s", reg, _req(compliance={"no_train_on_data": True})).dropped
    }
    assert "reducto" in dropped_strict

    reg2 = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, trains="unverified", text=CLEAN),
    )
    allowed = {
        "version": 1,
        "policy": {"allow_unverified_compliance": True},
        "strategies": {"s": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"}},
    }
    dropped_allowed = {
        d.backend
        for d in _compile(allowed, "s", reg2, _req(compliance={"no_train_on_data": True})).dropped
    }
    assert "reducto" not in dropped_allowed  # readmitted by the file policy


def test_confirmed_baa_tier_from_file_policy_readmits_and_warns():
    # a tier-gated BAA under require_baa is closed until the operator confirms the plan carries it
    def _reg():
        return scripted_registry(
            ScriptedBackend("reducto", hipaa_baa="tier_gated", cost_low=0.01, text=CLEAN)
        )

    strict = {"version": 1, "strategies": {"s": ["reducto"]}}
    with pytest.raises(ComplianceRefused):
        _compile(strict, "s", _reg(), _req(compliance={"require_baa": True}))

    confirmed = {
        "version": 1,
        "policy": {"baa_tier_confirmed": ["reducto"]},
        "strategies": {"s": ["reducto"]},
    }
    req = _req(compliance={"require_baa": True})
    res = _run(confirmed, "s", _reg(), req)
    note = next(w for w in res.response.warnings or [] if w.code == "baa_tier_confirmed")
    assert note.field == "reducto" and "tier_gated" in (note.message or "")


def test_policy_merge_carries_unnamed_router_config_fields_forward():
    # the file `policy:` fold rebuilds the RouterConfig; a field it does not name must survive
    # rather than silently reset to its default. The subclass stands in for a field added later.
    @dataclass
    class _FutureRouterConfig(RouterConfig):
        future_knob: str = ""

    base = _FutureRouterConfig(
        train_optout_confirmed=frozenset({"aws-textract"}), future_knob="set-by-the-deployment"
    )
    reg = scripted_registry(ScriptedBackend("pymupdf", local=True, text=CLEAN))
    cfg = {
        "version": 1,
        "policy": {"allow_unverified_compliance": True},
        "strategies": {"s": ["pymupdf"]},
    }
    compiled = _compile(cfg, "s", reg, _req(), base)

    merged = compiled.router_config
    assert merged.allow_unverified_compliance is True  # the file policy still OR-s in
    assert merged.train_optout_confirmed == frozenset({"aws-textract"})  # base still unions
    assert isinstance(merged, _FutureRouterConfig)
    assert merged.future_knob == "set-by-the-deployment"


# ---- limits -----------------------------------------------------------------------------------


def test_limits_does_not_touch_direct_named_requests(tmp_path, monkeypatch):
    # a config with a tiny time limit; a DIRECT-named backend bypasses the strategy layer, so the
    # ceiling never applies (it produces no orchestration and runs normally).
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "openreading.yaml").write_text(
        "version: 1\nlimits: { max_duration_per_doc: 1ms }\nstrategies:\n  cheap: [pymupdf]\n"
    )
    (tmp_path / "s.pdf").write_bytes(build_sample_pdf())
    out = api.run(str(tmp_path / "s.pdf"), backend="pymupdf")  # direct-named
    assert "orchestration" not in out  # legacy path — limits irrelevant


def test_file_level_zero_duration_ceiling_prunes_hedge():
    # BL-142: `limits: {max_duration_per_doc: "0ms"}` is a deliberate, schema-valid "no time
    # budget" operator ceiling. Before the fix, `compiled.max_duration_ms=0` was discarded by
    # run_strategy's `or`-chain (`0 or X` always evaluates `X`) and silently replaced with the full
    # DEFAULT_DEADLINE_MS (120s) — so a 5s hedge under this "zero budget" ceiling would never prune.
    # Mirrors test_start_after_past_deadline_is_pruned (tests/test_strategy_hedge.py), but drives
    # the ceiling through the file-level `limits:` block instead of a node-level `budget:`.
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),
    )
    cfg = {
        "version": 1,
        "limits": {"max_duration_per_doc": "0ms"},
        "strategies": {
            "s": {
                "parallel": ["pymupdf", {"backend": "reducto", "start_after": "5s"}],
                "pick": "best",
                "budget": {"max_cost_usd": 1.0},
            }
        },
    }
    compiled = _compile(cfg, "s", reg, _req())
    assert compiled.max_duration_ms == 0  # "0ms" parses to a real int zero, not None
    res = _run(cfg, "s", reg, _req())
    cats = [(a["backend"], a["category"]) for a in res.orchestration["attempts"]]
    assert ("reducto", "deadline_pruned") in cats


def test_file_level_nonzero_duration_ceiling_still_prunes_hedge():
    # the pre-existing truthy case must stay correct after the fix: a 1ms file-level ceiling still
    # prunes a 5s hedge exactly as it did before (proves the fix didn't touch the already-working
    # nonzero path).
    reg = scripted_registry(
        ScriptedBackend("pymupdf", local=True, text=CLEAN),
        ScriptedBackend("reducto", cost_low=0.01, text=CLEAN),
    )
    cfg = {
        "version": 1,
        "limits": {"max_duration_per_doc": "1ms"},
        "strategies": {
            "s": {
                "parallel": ["pymupdf", {"backend": "reducto", "start_after": "5s"}],
                "pick": "best",
                "budget": {"max_cost_usd": 1.0},
            }
        },
    }
    res = _run(cfg, "s", reg, _req())
    cats = [(a["backend"], a["category"]) for a in res.orchestration["attempts"]]
    assert ("reducto", "deadline_pruned") in cats
