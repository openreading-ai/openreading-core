"""BL-54 — `strategies/prune.py`'s two partial-prune corners for a `decide:` node
(`integration.md` §2c), neither of which had any regression coverage before this file:

1. The operator's configured `otherwise:` target is pruned while at least one `among:` member
   survives — the node becomes a decide over the survivors, with the first-listed survivor
   substituted as the new engine default (documented, deliberate behavior). This file also covers
   the `otherwise_pruned` downgrade signal that makes the substitution visible on the decision
   record, instead of it reading identically to "nothing unusual happened" (`downgraded: null`).
2. `among:` empties entirely (every member pruned) while `otherwise:` itself would have survived
   on its own — the WHOLE node still collapses (documented, deliberate behavior): a decide with
   nothing left to decide between never silently dispatches straight through.

Both scenarios are fully offline (compliance-driven pruning only, no live decider).
"""

from __future__ import annotations

import base64
import dataclasses
import os
import subprocess
import sys
import textwrap

import pytest

from openreading.adapters.registry import build_registry
from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.strategies.prune import _canonical_router_config
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ComplianceRefused
from openreading.types.request import OpenReadingRequest
from tests.fakes import ScriptedBackend, scripted_registry

CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3


def _req(compliance: dict | None = None) -> OpenReadingRequest:
    body: dict = {
        "document": {
            "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            "mime_type": "application/pdf",
        },
        "backend": {"id": "strategy:s"},
    }
    if compliance:
        body["compliance"] = compliance
    return OpenReadingRequest.model_validate(body)


def _candidate_id(node: dict) -> str:
    return node["backend"]


def test_otherwise_pruned_substitutes_first_among_survivor_and_traces_it():
    # otherwise: "b2" is structurally the SAME candidate as among[1] ("b2") — a non-first entry.
    # Under require_local only the hosted "b2" is dropped; "b1"/"b3" (local) both survive.
    b1 = ScriptedBackend("b1", local=True, text=CLEAN)
    b2 = ScriptedBackend("b2", local=False, cost_low=0.01, text=CLEAN)  # dropped by require_local
    b3 = ScriptedBackend("b3", local=True, text=CLEAN)
    reg = scripted_registry(b1, b2, b3)
    r = _req(compliance={"require_local": True})
    cfg = StrategyConfig.model_validate(
        {
            "version": 1,
            "strategies": {"s": {"decide": {"among": ["b1", "b2", "b3"], "otherwise": "b2"}}},
        }
    )
    compiled = compile_strategy(r, "s", cfg, reg, RouterConfig())

    # integration.md §2c: among[1] ("b2") is dropped, so the pruned among: is [b1, b3] and the new
    # otherwise: is the survivor now at index 0 ("b1") — asserted directly on the compiled tree.
    pruned_decide = compiled.root["decide"]
    assert [_candidate_id(m) for m in pruned_decide["among"]] == ["b1", "b3"]
    assert _candidate_id(pruned_decide["otherwise"]) == "b1"
    assert pruned_decide.get("otherwise_pruned") is True

    res = run_strategy(compiled, r, registry=reg, broker=EnvCredentialBroker(), clock=FakeClock())

    # ...and the dispatched backend is exactly that survivor.
    assert res.response.backend.id == "b1"
    decisions = res.orchestration["decisions"]
    assert len(decisions) == 1
    d = decisions[0]
    assert d["point"] == "decide"
    assert d["chosen"] == "otherwise"
    assert d["decider"] == "engine"
    # BL-54: the substitution is no longer invisible on the decision record.
    assert d["downgraded"] == "otherwise_pruned"
    # the drop itself is still independently recorded too (unchanged, pre-existing behavior).
    assert any(dr["backend"] == "b2" for dr in res.orchestration["dropped"])
    # the pruned backend was never dispatched.
    assert len(b2.contexts) == 0


def test_among_fully_pruned_otherwise_alone_survives_collapses_the_whole_node():
    # among: ["b1", "b2"] are both hosted (dropped by require_local); otherwise: "b3" is local and
    # would survive on its own — but integration.md §2c says the WHOLE node still collapses (a
    # decide with nothing left to decide between is not silently downgraded to a bare dispatch).
    # Nested in a cascade so the collapse is externally observable: the decide node vanishes
    # entirely and "fallback" (a distinct backend) runs instead.
    b1 = ScriptedBackend("b1", local=False, cost_low=0.01, text=CLEAN)
    b2 = ScriptedBackend("b2", local=False, cost_low=0.01, text=CLEAN)
    b3 = ScriptedBackend("b3", local=True, text=CLEAN)
    fallback = ScriptedBackend("fallback", local=True, text="a distinct fallback body " * 5)
    reg = scripted_registry(b1, b2, b3, fallback)
    r = _req(compliance={"require_local": True})
    cfg = StrategyConfig.model_validate(
        {
            "version": 1,
            "strategies": {
                "s": {"steps": [{"decide": {"among": ["b1", "b2"], "otherwise": "b3"}}, "fallback"]}
            },
        }
    )
    compiled = compile_strategy(r, "s", cfg, reg, RouterConfig())

    # the decide node returns None (prune.py:259) and is dropped from the cascade's own steps —
    # only the "fallback" leaf remains in the compiled tree.
    assert compiled.root["steps"] == [{"backend": "fallback"}]

    res = run_strategy(compiled, r, registry=reg, broker=EnvCredentialBroker(), clock=FakeClock())

    assert res.response.backend.id == "fallback"
    # no decision was ever recorded for the collapsed decide node — it never dispatched b3 either,
    # i.e. it did NOT silently dispatch straight through with no decision recorded.
    assert res.orchestration["decisions"] == []
    assert len(b1.contexts) == 0
    assert len(b2.contexts) == 0
    assert len(b3.contexts) == 0


# --- BL-168: compile-time set iteration is deterministic across PYTHONHASHSEED -----------------


def test_dropped_order_is_deterministic_across_hash_seeds():
    """`all_names = set(config.strategies) | PRESET_NAMES | {name}` used to iterate in a
    hash-seed-dependent order, which reached `trace.dropped`, `orchestration["dropped"]`, and the
    ComplianceRefused message — so the explanation shown for *why a compliant run was refused* was
    not reproducible (internal/runs/ledger-defect-worklist.md item 4, repro: 8 seeds -> 8 distinct
    orderings). Runs one fixed walk with several real backends dropped under `require_local`, in a
    fresh subprocess per seed 0-4 (precedent: tests/test_strategy_surface.py's subprocess-script
    pattern), and asserts byte-identical output."""
    script = textwrap.dedent("""
        import json
        from openreading.adapters.registry import build_registry
        from openreading.router.router import RouterConfig
        from openreading.strategies.model import StrategyConfig
        from openreading.strategies.prune import compile_strategy
        from openreading.types.request import OpenReadingRequest

        registry = build_registry()
        req = OpenReadingRequest.model_validate({
            "document": {"path": "/d.pdf", "mime_type": "application/pdf"},
            "backend": {"id": "auto"},
            "compliance": {"require_local": True},
        })
        # Several DISTINCT strategy names, each referencing a different dropped backend, so which
        # strategy's tree gets walked first (i.e. `all_names`' set-iteration order) determines
        # dropped_records' insertion order — a single strategy naming every backend doesn't
        # reproduce the bug, since its own steps-list order is already deterministic.
        config = StrategyConfig.model_validate({
            "version": 1,
            "strategies": {
                "cheap": {"steps": ["pymupdf"]},
                "alpha": {"steps": ["azure-document-intelligence"]},
                "beta": {"steps": ["aws-textract"]},
                "gamma": {"steps": ["anthropic-claude"]},
                "delta": {"steps": ["google-document-ai"]},
                "epsilon": {"steps": ["reducto"]},
            },
        })
        plan = compile_strategy(req, "cheap", config, registry, RouterConfig())
        print(json.dumps([{"backend": d.backend, "code": d.code} for d in plan.dropped]))
    """)
    outputs = []
    for seed in range(5):
        env = dict(os.environ, PYTHONHASHSEED=str(seed))
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, env=env
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout)
    assert len(outputs[0].strip()) > 0  # sanity: something was actually dropped
    assert len(set(outputs)) == 1, outputs


def test_compliance_refused_message_names_dropped_backends_in_sorted_order():
    # BL-168 review (trent): the ComplianceRefused message sorts dropped_records the same way
    # CompiledPlan.dropped does, but nothing previously exercised that branch with more than one
    # dropped backend — the exact user-facing scenario the defect was about: "the explanation
    # shown for why a compliant run was refused." "zulu"/"alpha" are deliberately out of
    # alphabetical declaration order so a correct fix can only pass by actually sorting.
    zulu = ScriptedBackend("zulu", local=False, cost_low=0.01, text=CLEAN)
    alpha = ScriptedBackend("alpha", local=False, cost_low=0.01, text=CLEAN)
    reg = scripted_registry(zulu, alpha)
    r = _req(compliance={"require_local": True})
    cfg = StrategyConfig.model_validate(
        {"version": 1, "strategies": {"s": {"steps": ["zulu", "alpha"]}}}
    )
    with pytest.raises(ComplianceRefused) as exc:
        compile_strategy(r, "s", cfg, reg, RouterConfig())
    msg = str(exc.value)
    assert "alpha" in msg and "zulu" in msg
    assert msg.index("alpha") < msg.index("zulu")


# --- BL-163: config_hash is compliance-aware, not just root-shape-aware -------------------------


def test_config_hash_distinguishes_five_compliance_postures():
    """internal/runs/ledger-defect-worklist.md item 5's own repro table: the same strategy compiled under
    five mutually-distinguishable compliance postures used to collapse to 4 distinct config_hash
    values (require_baa and require_baa+baa_tier_confirmed={reducto} shared one digest with "no
    compliance at all" whenever they happened to prune the same leaves) — confirmed by stashing
    the fix out and rerunning this exact scenario. All five must now produce distinct digests,
    using the real registry (real descriptors, real compliance profiles) the same way the
    worklist's own repro did."""
    registry = build_registry()
    config = StrategyConfig.model_validate(
        {
            "version": 1,
            "strategies": {
                "cheap": {
                    "steps": [
                        "pymupdf",
                        "docling",
                        "azure-document-intelligence",
                        "aws-textract",
                        "reducto",
                        "tesseract",
                    ]
                },
            },
        }
    )

    def _req(compliance=None):
        body = {
            "document": {"path": "/d.pdf", "mime_type": "application/pdf"},
            "backend": {"id": "auto"},
        }
        if compliance:
            body["compliance"] = compliance
        return OpenReadingRequest.model_validate(body)

    postures = [
        (None, RouterConfig()),
        ({"require_baa": True}, RouterConfig()),
        ({"require_local": True}, RouterConfig()),
        ({"data_region": "eu-west-1"}, RouterConfig()),
        ({"require_baa": True}, RouterConfig(baa_tier_confirmed=frozenset({"reducto"}))),
    ]
    hashes = [
        compile_strategy(_req(compliance), "cheap", config, registry, rc).config_hash
        for compliance, rc in postures
    ]
    assert len(set(hashes)) == 5, hashes


def test_config_hash_changes_with_train_optout_confirmed_too():
    # The worklist's own repro table only exercises baa_tier_confirmed — this locks that
    # RouterConfig's OTHER frozenset field also participates in the hash, and that
    # _canonical_router_config (BL-163 review, alex: derived generically from dataclasses.fields,
    # not hand-enumerated) isn't accidentally only covering one of the two.
    registry = build_registry()
    config = StrategyConfig.model_validate(
        {"version": 1, "strategies": {"cheap": {"steps": ["aws-textract", "pymupdf"]}}}
    )
    req = OpenReadingRequest.model_validate(
        {
            "document": {"path": "/d.pdf", "mime_type": "application/pdf"},
            "backend": {"id": "auto"},
        }
    )
    plain = compile_strategy(req, "cheap", config, registry, RouterConfig())
    confirmed = compile_strategy(
        req, "cheap", config, registry, RouterConfig(train_optout_confirmed=frozenset({"aws"}))
    )
    assert plain.config_hash != confirmed.config_hash


def test_canonical_router_config_raises_on_an_unrecognized_field_type():
    # BL-163 round-2 review (jay, Medium, dormant): the dispatch rule only handles
    # frozenset/set/primitives today (RouterConfig's actual current fields); a future field of
    # some other shape (a nested dict, a nested dataclass) must FAIL LOUD at compile time here,
    # not silently fall through to json.dumps's default=str fallback — whose repr could hash a
    # future field's value nondeterministically, the exact bug class BL-168 fixed through a
    # different door. Probes the function directly with a stand-in dataclass (it only relies on
    # dataclasses.fields()/getattr, not RouterConfig specifically) rather than waiting for
    # RouterConfig to actually grow such a field.
    @dataclasses.dataclass
    class _FutureRouterConfigShape:
        allow_unverified_compliance: bool = False
        nested: dict | None = None

    with pytest.raises(TypeError, match="doesn't know how to hash"):
        _canonical_router_config(_FutureRouterConfigShape(nested={"a": 1}))


def test_config_hash_never_contains_a_resolved_credential():
    # BL-163's own "do not": credentials_ref values, resolved credentials, or anything
    # secret-bearing must never enter the digest input. Setting a (structurally distinguishing)
    # credentials_ref must NOT change config_hash — it isn't a compliance-eligibility input, and
    # no descriptor's to_schema_dict() carries a live secret (descriptors are static
    # capability/compliance metadata, never a resolved credential value).
    registry = build_registry()
    config = StrategyConfig.model_validate(
        {"version": 1, "strategies": {"cheap": {"steps": ["pymupdf"]}}}
    )

    def _req(**backend_extra):
        body = {
            "document": {"path": "/d.pdf", "mime_type": "application/pdf"},
            "backend": {"id": "auto", **backend_extra},
        }
        return OpenReadingRequest.model_validate(body)

    plain = compile_strategy(_req(), "cheap", config, registry, RouterConfig())
    with_ref = compile_strategy(
        _req(credentials_ref="env:SOME_ALIAS"), "cheap", config, registry, RouterConfig()
    )
    assert plain.config_hash == with_ref.config_hash
