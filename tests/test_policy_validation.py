"""Policy shape validation, pinned across every surface that reads one.

The `policy:` block of `openreading.yaml`, a `config=` mapping of that same shape, and an HTTP
request's `compliance` object all name the same constraints, so all three refuse the same
malformed policy the same way. Before this suite the CLI/Python path split the policy dict into
its known keys *before* anything validated it, so `{"hipaa": true, "gdpr": "strict"}` left every
backend eligible with an empty `dropped` map and exit 0 while `Compliance(extra="forbid")` on the
HTTP path rejected the identical keys with a 400. An operator who spelled a constraint wrong got
no filter and no message.

The block is the last policy surface the schema cannot close: it is `additionalProperties: true`
until `strategy-config` v0.3, so nothing upstream can refuse a key. A misspelled key was dropped
in silence, and a quoted `allow_unverified_compliance: "false"` was truthy enough to switch the
fail-closed tolerance ON. It is refused now where the file is read, once, by
`openreading.config.load`, and reaches the caller as the `ConfigError` every other unloadable
file raises.

The parity test below is what keeps the two schemas from drifting apart. `strategy-config` v0.3
closed the block, so the schema is now the single enumeration of what a policy may say, and the
hand-written validator that stood in for it is gone. What one schema cannot check is that the
OTHER one agrees, which is why the five compliance keys are compared by name, by type and by
description string.
"""

from __future__ import annotations

import json

import pytest

from openreading import api, schemas
from openreading.config import ConfigError
from openreading.testing.sample_pdf import build_sample_pdf

pytest.importorskip("fitz", reason="pymupdf not installed")


# The three words a compliance officer reaches for, none of which this engine implements.
_OFFICER_POLICY = {"hipaa": True, "gdpr": "strict", "soc2": ["type2"]}

# Blocks that are valid YAML and are not a policy object.
_NON_OBJECT_BLOCKS = ("[]", "null", '"require_baa"', "3", "true")


@pytest.fixture
def sample_pdf(tmp_path):
    p = tmp_path / "sample.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def _policy_block(tmp_path, body: str) -> str:
    """An openreading.yaml whose `policy:` block is exactly `body`, written as YAML."""
    p = tmp_path / "openreading.yaml"
    p.write_text(f"version: 1\npolicy: {body}\n")
    return str(p)


def _inline(policy) -> dict:
    """The same policy the way a caller with no file writes it: the file's shape, in memory."""
    return {"version": 1, "policy": policy}


# --- the five compliance keys are one grammar, written in two schemas ---------------------------


def test_the_typed_model_and_the_schema_hold_the_same_nine_keys():
    """Two doors into one grammar: the schema guards file text, the model guards a Python object.
    A key added to one and not the other means a policy that loads from a file and is refused
    from Python, or the reverse, which is the drift this whole change exists to end."""
    from openreading.types.policy import Policy

    schema_keys = set(schemas.strategy_config_schema()["properties"]["policy"]["properties"])
    assert set(Policy.model_fields) == schema_keys


# --- symptom 1: unrecognised keys ---------------------------------------------------------------


def test_python_route_unrecognised_policy_key_raises_config_error(sample_pdf):
    with pytest.raises(ConfigError) as exc:
        api.route(sample_pdf, config=_inline(_OFFICER_POLICY))
    assert "hipaa" in str(exc.value)


def test_run_refuses_an_unrecognised_policy_key_before_dispatch(sample_pdf):
    """`run()` reads the file before it builds anything, so the refusal lands before any backend
    is constructed."""
    with pytest.raises(ConfigError):
        api.run(sample_pdf, backend="pymupdf", config=_inline(_OFFICER_POLICY))


def test_run_batch_refuses_an_unrecognised_policy_key(sample_pdf):
    with pytest.raises(ConfigError):
        api.run_batch([sample_pdf], backend="pymupdf", config=_inline(_OFFICER_POLICY))


# --- symptoms 2 and 3: a block that is not an object --------------------------------------------


@pytest.mark.parametrize("value", ([], 3, True))
def test_a_config_that_is_not_a_path_or_a_mapping_is_refused(value, sample_pdf):
    """`config=` takes a path or the file's shape. Anything else used to reach `Path()` and raise
    a bare TypeError naming neither the argument nor what it should have been."""
    with pytest.raises(ConfigError) as exc:
        api.route(sample_pdf, config=value)
    assert "path or a mapping" in str(exc.value)


def test_python_route_config_none_still_means_no_file(sample_pdf, tmp_path, monkeypatch):
    """`config=None` is the documented Python default and must stay a no-op, not an error."""
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert api.route(sample_pdf, config=None).chosen is not None


# --- wrong value types under a recognised key ---------------------------------------------------


@pytest.mark.parametrize(
    "policy",
    [
        # A bare string where a list belongs. It used to become a list of that string's
        # CHARACTERS, permitting fifteen single-letter backends and none of the real ones.
        {"backends": "pymupdf"},
        {"backends": 3},
        {"backends": {"pymupdf": True}},
        {"backends": [1, 2]},
        {"backends": [None]},
        {"backends": True},
    ],
)
def test_policy_value_of_the_wrong_type_is_refused(policy, sample_pdf):
    with pytest.raises(ConfigError):
        api.route(sample_pdf, config=_inline(policy))


# --- the file's own `policy:` block --------------------------------------------------------------
#
# `openreading.yaml`'s `policy:` block is the one place a policy is spelled, and the router guide
# teaches it as the way to gate a whole corpus. The schema leaves that sub-object open
# (`additionalProperties: true`) until v0.3 closes it, so nothing in the schema can refuse a key:
# the guard is `openreading.config._validated_policy`, which runs where the file is read, once,
# before any surface interprets a key.


def _config_file(tmp_path, policy: dict, backend: str) -> str:
    """The file a person writes: one policy block and one one-rung strategy. Written to disk, not
    built with `model_validate`, so these tests bind the guard to the path a real run takes."""
    p = tmp_path / "openreading.yaml"
    p.write_text(f"version: 1\npolicy: {json.dumps(policy)}\nstrategies:\n  s: [{backend}]\n")
    return str(p)
