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

import pytest

from openreading.adapters.registry import build_registry
from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.router import RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ScopeRefused
from openreading.types.request import OpenReadingRequest

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


# --- BL-168: compile-time set iteration is deterministic across PYTHONHASHSEED -----------------


# --- BL-163: config_hash is compliance-aware, not just root-shape-aware -------------------------


# --- caller backend allow-list (the scope a bearer token carries) ------------------------------
#
# `compile_strategy(backend_allowlist=...)` is where a `strategy:<name>` walk is bounded to the
# backends its caller may reach. Before it existed, a strategy id was exempt from the check the
# server ran on a directly-named backend, so naming any strategy — including a preset, which needs
# no config file — reached every backend that strategy's rungs touch. These cover the mechanism
# itself, at the layer that implements it; the end-to-end HTTP behaviour is in test_server.py.


def test_allowlist_prunes_an_out_of_scope_rung_and_keeps_the_in_scope_one():
    registry = build_registry()
    config = StrategyConfig(version=1, strategies={"s": {"steps": ["pymupdf", "tesseract"]}})
    compiled = compile_strategy(
        _req(), "s", config, registry, RouterConfig(), backend_allowlist=frozenset({"pymupdf"})
    )
    assert [n["backend"] for n in compiled.root["steps"]] == ["pymupdf"]
    assert {d.backend: d.code for d in compiled.dropped}["tesseract"] == "scope_denied"


def test_scope_refusal_names_a_backend_the_requested_strategy_actually_names():
    # compile_strategy prunes every OTHER strategy and preset in the same pass so `use:` refs
    # resolve. Their drops must not be what the refusal reports: "cheap" names only pymupdf, so
    # naming docling (which only a preset mentions) would send the reader to the wrong rung.

    registry = build_registry()
    config = StrategyConfig(version=1, strategies={"cheap": {"steps": ["pymupdf"]}})
    with pytest.raises(ScopeRefused) as e:
        compile_strategy(
            _req(),
            "cheap",
            config,
            registry,
            RouterConfig(),
            backend_allowlist=frozenset({"tesseract"}),
        )
    assert e.value.backend_code == "pymupdf"
    assert "docling" not in str(e.value)


def test_no_allowlist_compiles_byte_for_byte_as_before():
    registry = build_registry()
    config = StrategyConfig(version=1, strategies={"s": {"steps": ["pymupdf", "tesseract"]}})
    a = compile_strategy(_req(), "s", config, registry, RouterConfig())
    b = compile_strategy(_req(), "s", config, registry, RouterConfig(), backend_allowlist=None)
    assert a.root == b.root and a.eligible == b.eligible and a.config_hash == b.config_hash
    assert not a.dropped


def _bare_walk_ctx():
    """A minimal _WalkCtx for the resolver, which reads only eligible/attempted/allow-list."""
    from openreading.strategies.engine import _WalkCtx
    from openreading.strategies.trace import Trace

    return _WalkCtx(
        req=_req(),
        registry=build_registry(),
        broker=EnvCredentialBroker(),
        clock=FakeClock(),
        trace=Trace(strategy="s", config_hash=""),
        trees={},
    )


def test_allowlist_that_empties_the_tree_refuses_as_scope_not_compliance():
    """Which exception this is decides which file the operator goes and edits.

    Restored after the removal set deleted it. It was swept up with the compliance tests because
    its name says "not compliance", but its subject is the API-key scope, which is now the ONLY
    hard boundary in this package. `ComplianceRefused` is gone, so the contrast it drew is simply
    that the refusal is `ScopeRefused` and names the backend the token cannot reach.
    """
    registry = build_registry()
    config = StrategyConfig(version=1, strategies={"s": {"steps": ["pymupdf"]}})

    with pytest.raises(ScopeRefused) as e:
        compile_strategy(
            _req(), "s", config, registry, RouterConfig(), backend_allowlist=frozenset({"docling"})
        )

    assert e.value.backend_code == "pymupdf"


def test_dispatchable_is_exactly_what_the_tree_names():
    """`dispatchable` arms the sanitizer and `pinned_eligible`, so it must be tight.

    It used to be the union of the tree's named ids and, when any leaf said `auto`, the whole
    candidate chain — because `auto` could resolve to any of them at dispatch. `auto` is gone, so
    a compiled tree can only ever dispatch what it names, and widening this beyond that would
    admit a backend to the sanitizer no node can reach.
    """
    registry = build_registry()
    config = StrategyConfig.model_validate(
        {
            "version": 1,
            "strategies": {
                "s": {
                    "parallel": [{"backend": "pymupdf"}, {"backend": "tesseract"}],
                    "pick": "best",
                }
            },
        }
    )

    compiled = compile_strategy(_req(), "s", config, registry, RouterConfig(backends=("pymupdf",)))

    assert compiled.eligible == ["pymupdf"]
    assert compiled.dispatchable == ["pymupdf", "tesseract"]


def test_dispatchable_includes_named_leaves_outside_the_dynamic_chain():
    """A named leaf runs directly. `eligible` is what an UNNAMED request would resolve to, and a
    strategy naming a backend outside it is not a conflict, because naming one is explicit."""
    registry = build_registry()
    config = StrategyConfig.model_validate(
        {"version": 1, "strategies": {"s": {"steps": [{"backend": "tesseract"}]}}}
    )

    compiled = compile_strategy(_req(), "s", config, registry, RouterConfig(backends=("pymupdf",)))

    assert compiled.eligible == ["pymupdf"]
    assert compiled.dispatchable == ["tesseract"]
