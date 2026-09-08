"""Invariant/guardrail tests (GOAL3 §4 "Invariant tests", §5 guardrails). These are generic
property-style checks that hold WHATEVER the tree shape — a pruned (non-compliant) backend can
never be dispatched anywhere, and the grammar cannot express a retry knob.
"""

from __future__ import annotations

import base64

import pytest

from openreading.schemas import validate_strategy_config
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.request import OpenReadingRequest

CLEAN = "the quick brown fox jumps over the lazy dog every day here and now again " * 3


def _req():
    return OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
                "mime_type": "application/pdf",
            },
            "backend": {"id": "strategy:s"},
            # HOSTED backends must be pruned everywhere
        }
    )


# every tree shape places a NON-compliant hosted backend ("hosted") somewhere; under require_local
# it must be pruned and never dispatched, whatever the node type (spec §5 guardrail 3).
_TREES = {
    "cascade": {"steps": ["hosted", "pymupdf"]},
    "cascade_hosted_first": {"steps": ["pymupdf", "hosted"]},
    "parallel_best": {
        "parallel": ["hosted", "pymupdf"],
        "pick": "best",
        "budget": {"max_cost_usd": 1.0},
    },
    "parallel_merge": {
        "parallel": ["hosted", "pymupdf"],
        "pick": "merge",
        "budget": {"max_cost_usd": 1.0},
    },
    "route": {
        "route": {
            "rules": [{"when": {"mime": "application/pdf"}, "use": "hosted"}],
            "default": "pymupdf",
        }
    },
    "decide": {"decide": {"among": ["hosted", "pymupdf"], "otherwise": "pymupdf"}},
    "judge": {
        "parallel": ["hosted", "pymupdf"],
        "pick": "best",
        "judge": {"backend": "hosted"},
        "budget": {"max_cost_usd": 1.0},
    },
}


# ---- no retry knobs are expressible in the grammar (spec §5 guardrail 4) ----------------------


@pytest.mark.parametrize(
    "knob",
    [
        {"backend": "pymupdf", "max_retries": 3},
        {"backend": "pymupdf", "retries": 3},
        {"backend": "pymupdf", "retry": {"attempts": 2}},
        {"steps": ["pymupdf"], "max_attempts_per_backend": 2},
    ],
)
def test_retry_knobs_are_rejected_by_the_schema(knob):
    # additionalProperties:false on every node means a retry knob is a structural impossibility —
    # same-backend retry is the driver's alone (D-v2-7.2); a RetryableError at the engine advances.
    with pytest.raises(Exception):  # noqa: B017 — jsonschema.ValidationError
        validate_strategy_config({"version": 1, "strategies": {"s": knob}})
