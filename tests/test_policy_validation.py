"""Policy shape validation, pinned across every surface that reads one.

A `--policy` file, a `policy=` kwarg, an HTTP request's `compliance` object and the `policy:` block
of an `openreading.yaml` all name the same constraints, so all four refuse the same malformed
policy the same way. Before this suite the CLI/Python path split the policy dict into its known
keys *before* anything validated it, so `{"hipaa": true, "gdpr": "strict"}` left every backend
eligible with an empty `dropped` map and exit 0 while `Compliance(extra="forbid")` on the HTTP path
rejected the identical keys with a 400. An operator who spelled a constraint wrong got no filter
and no message.

The file `policy:` block was the last reader to trust its dict, and the schema leaves that
sub-object open on purpose, so nothing upstream could refuse it: a misspelled key was dropped in
silence, and a quoted `allow_unverified_compliance: "false"` was truthy enough to switch the
fail-closed tolerance ON. It is refused now where the file is read, once, by
`openreading.config.load`, and reaches the caller as the `ConfigError` every other unloadable
file raises.

The single-enumeration test below is the guard that keeps the key set from drifting again: it is
derived from the models the keys actually feed, never re-typed beside them. Only part of that test
can fail, though: the explicit three-name pin on `ROUTER_CONFIG_POLICY_KEYS` bites when a fourth
field arrives, while its three set-equality assertions restate the very expressions that define the
constants, so they record the intent rather than catch a violation of it, and the derivation in
`api.py` is what actually prevents the drift.
"""

from __future__ import annotations

import json

import pytest

from openreading import api
from openreading.router.compliance import RouterConfig
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ComplianceRefused
from openreading.types.request import Compliance, Routing

pytest.importorskip("fitz", reason="pymupdf not installed")

from openreading.cli import main  # noqa: E402

# The three words a compliance officer reaches for, none of which this engine implements.
_OFFICER_POLICY = {"hipaa": True, "gdpr": "strict", "soc2": ["type2"]}

# Top-level shapes that are valid JSON but are not a policy object.
_NON_OBJECT_BODIES = ("[]", "null", '"require_baa"', '"strict"', "3", "true")


@pytest.fixture
def sample_pdf(tmp_path):
    p = tmp_path / "sample.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def _policy_file(tmp_path, body: str):
    p = tmp_path / "policy.json"
    p.write_text(body)
    return str(p)


# --- the key set has exactly one enumeration --------------------------------------------------


def test_policy_keys_are_derived_from_the_models_they_feed():
    """Every policy key is a field of the model it lands in, and every such field is a policy key.

    A hand-maintained second list of compliance keys is how a key gets added to the router and
    silently dropped by the policy loader (or the reverse), so nothing here may be re-typed.
    """
    assert tuple(Compliance.model_fields) == api.COMPLIANCE_POLICY_KEYS
    assert set(api.ROUTING_POLICY_KEYS) <= set(Routing.model_fields)
    assert tuple(RouterConfig.__dataclass_fields__) == api.ROUTER_CONFIG_POLICY_KEYS
    assert set(api.POLICY_KEYS) == (
        set(api.COMPLIANCE_POLICY_KEYS)
        | set(api.ROUTING_POLICY_KEYS)
        | set(api.ROUTER_CONFIG_POLICY_KEYS)
    )
    # `routing.fallback` is a request field, not a policy key: a policy names constraints, not the
    # chain order. Pinned so a future widening of the policy grammar is a deliberate edit.
    assert "fallback" not in api.POLICY_KEYS
    # `RouterConfig` is a dataclass, so its three keys are the only ones `validate_policy`
    # type-checks by hand. Adding a field to it makes that key a policy key automatically but does
    # NOT give it a check, so this pin fails until the author writes one.
    assert set(api.ROUTER_CONFIG_POLICY_KEYS) == {
        "allow_unverified_compliance",
        "train_optout_confirmed",
        "baa_tier_confirmed",
    }


# --- symptom 1: unrecognised keys ---------------------------------------------------------------


def test_cli_route_unrecognised_policy_key_exits_3_and_names_the_key(sample_pdf, tmp_path, capsys):
    """`{"hipaa": true, "gdpr": ..., "soc2": ...}` used to route 13 backends with `dropped: {}`."""
    policy = _policy_file(tmp_path, json.dumps(_OFFICER_POLICY))
    rc = main(["route", sample_pdf, "--policy", policy])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[route] invalid policy ")
    for key in _OFFICER_POLICY:
        assert repr(key) in err


def test_python_route_unrecognised_policy_key_raises_policy_error(sample_pdf):
    with pytest.raises(api.PolicyError) as exc:
        api.route(sample_pdf, policy=_OFFICER_POLICY)
    assert "'hipaa'" in str(exc.value)


def test_misspelled_policy_key_suggests_the_key_that_was_meant(sample_pdf):
    """`require_baaa` is the reported typo: the whole compliance filter vanished without a word."""
    with pytest.raises(api.PolicyError) as exc:
        api.route(sample_pdf, policy={"require_baaa": True})
    assert "did you mean 'require_baa'" in str(exc.value)


def test_run_refuses_an_unrecognised_policy_key_before_dispatch(sample_pdf):
    """`run()` shares `build_request`, so the refusal lands before any backend is constructed."""
    with pytest.raises(api.PolicyError):
        api.run(sample_pdf, backend="pymupdf", policy=_OFFICER_POLICY)


def test_run_batch_refuses_an_unrecognised_policy_key(sample_pdf):
    with pytest.raises(api.PolicyError):
        api.run_batch([sample_pdf], backend="pymupdf", policy=_OFFICER_POLICY)


def test_every_policy_taking_subcommand_refuses_the_same_file(tmp_path, capsys):
    """The refusal lives in the shared `--policy` loader, not in `cmd_route`, so a subcommand that
    never routes a document refuses the same file under its own tag."""
    config = tmp_path / "openreading.yaml"
    config.write_text("version: 1\nstrategies:\n  x: [pymupdf]\n")
    policy = _policy_file(tmp_path, json.dumps(_OFFICER_POLICY))
    rc = main(["strategy", "validate", "--config", str(config), "--policy", policy])
    assert rc == 3
    assert capsys.readouterr().err.startswith("[strategy validate] invalid policy ")


# --- symptoms 2 and 3: wrong top-level shape ----------------------------------------------------


@pytest.mark.parametrize("body", _NON_OBJECT_BODIES)
def test_cli_route_non_object_policy_exits_3_without_a_traceback(
    body, sample_pdf, tmp_path, capsys
):
    """`[]` / `null` used to run with no filter at all; a JSON string crashed with a raw TypeError
    (`_apply_policy`) or AttributeError (`router_config`) depending on its content."""
    policy = _policy_file(tmp_path, body)
    rc = main(["route", sample_pdf, "--policy", policy])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[route] invalid policy ")
    assert len(err.splitlines()) == 1
    assert "Traceback" not in err


@pytest.mark.parametrize("policy", ([], "require_baa", "strict", 3, True))
def test_python_route_non_object_policy_raises_policy_error(policy, sample_pdf):
    with pytest.raises(api.PolicyError) as exc:
        api.route(sample_pdf, policy=policy)
    assert "JSON object" in str(exc.value)


def test_python_route_policy_none_still_means_no_policy(sample_pdf):
    """`policy=None` is the documented Python default and must stay a no-op, not an error."""
    assert api.route(sample_pdf, policy=None).chosen is not None


# --- wrong value types under a recognised key ---------------------------------------------------


@pytest.mark.parametrize(
    "policy",
    [
        {"require_baa": "yes"},
        {"optimize_for": "speed"},
        {"data_region": ["eu"]},
        # a truthy STRING under the one key that widens the eligible set: `bool("false")` is True.
        {"allow_unverified_compliance": "false"},
        # a bare string used to become a frozenset of its CHARACTERS, confirming no backend.
        {"train_optout_confirmed": "aws-textract"},
        {"baa_tier_confirmed": "reducto"},
    ],
)
def test_policy_value_of_the_wrong_type_is_refused(policy, sample_pdf):
    with pytest.raises(api.PolicyError):
        api.route(sample_pdf, policy=policy)


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


def test_file_policy_unrecognised_key_is_refused_not_dropped(tmp_path):
    """`require_locall` used to be discarded in silence: the run proceeded with NO locality
    constraint at all, and a hosted rung stayed eligible until it died on credentials instead."""
    from openreading import config

    with pytest.raises(config.ConfigError) as exc:
        config.load(_config_file(tmp_path, {"require_locall": True}, "reducto"))
    assert "did you mean 'require_local'" in str(exc.value)


def test_file_policy_correctly_spelled_still_refuses_the_backend(sample_pdf, tmp_path):
    """The other half of the pair: the guard must refuse the typo WITHOUT changing what a
    correctly spelled policy does — `require_local` still drops the hosted rung on compliance."""
    path = _config_file(tmp_path, {"require_local": True}, "reducto")
    with pytest.raises(ComplianceRefused) as exc:
        api.run(sample_pdf, strategy="s", config=path)
    assert "reducto:not_local" in str(exc.value)


def test_a_named_backend_is_gated_by_the_file_block_too(sample_pdf, tmp_path):
    """Law P4's own case: a run that names a backend reads the same block a strategy run does.
    Before the file was read on every path this run ignored the operator's policy entirely."""
    path = _config_file(tmp_path, {"require_local": True}, "pymupdf")
    with pytest.raises(ComplianceRefused) as exc:
        api.run(sample_pdf, backend="reducto", config=path)
    assert "require_local" in str(exc.value)


def test_file_policy_string_cannot_flip_the_fail_closed_switch(tmp_path):
    """The compliance defect: `allow_unverified_compliance: "false"` is truthy to `bool()`, so a
    quoted `false` — whose plain-English intent is *off* — used to switch the tolerance ON and
    admit a `trains_on_customer_data: unverified` backend under `no_train_on_data: true`."""
    from openreading import config

    policy = {"no_train_on_data": True, "allow_unverified_compliance": "false"}
    with pytest.raises(config.ConfigError) as exc:
        config.load(_config_file(tmp_path, policy, "nuextract"))
    assert "allow_unverified_compliance must be true or false" in str(exc.value)


def test_file_policy_unquoted_false_keeps_the_unverified_backend_out(sample_pdf, tmp_path):
    """Two spellings of the same policy now agree: the boolean refuses `nuextract` on compliance,
    the string refuses the policy itself. Neither admits it."""
    policy = {"no_train_on_data": True, "allow_unverified_compliance": False}
    path = _config_file(tmp_path, policy, "nuextract")
    with pytest.raises(ComplianceRefused) as exc:
        api.run(sample_pdf, strategy="s", config=path)
    assert "nuextract:trains_unverified" in str(exc.value)


def test_the_guard_sits_in_the_shared_fold_not_in_its_callers(sample_pdf):
    """`calibrate_strategy` folds the file `policy:` block by calling these two helpers directly,
    never through `openreading.config.load` (it is documented as reusing them rather than
    reimplementing them). A guard wired into callers is exactly what left this reader uncovered
    the first time, so it lives in the two functions that turn a raw dict into a constraint."""
    from openreading.config import merge_router_config, union_compliance

    with pytest.raises(api.PolicyError):
        union_compliance(None, {"require_locall": True})
    with pytest.raises(api.PolicyError):
        merge_router_config(RouterConfig(), {"allow_unverified_compliance": "false"})


def test_cli_strategy_plan_refuses_a_malformed_file_policy(sample_pdf, tmp_path, capsys):
    """A malformed file policy, end to end: one tagged stderr line and exit 3, the same ladder
    rung a malformed `--policy` file already got."""
    config = tmp_path / "openreading.yaml"
    config.write_text(
        'version: 1\npolicy: {no_train_on_data: true, allow_unverified_compliance: "false"}\n'
        "strategies:\n  s: [nuextract]\n"
    )
    rc = main(["strategy", "plan", sample_pdf, "--strategy", "s", "--config", str(config)])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[strategy plan] ")
    assert "invalid policy: " in err
    assert len(err.splitlines()) == 1
    assert "Traceback" not in err
    assert "nuextract" not in capsys.readouterr().out


def test_cli_strategy_validate_reports_a_malformed_file_policy(tmp_path, capsys):
    """`strategy validate` is the surface whose whole job is finding this before a run does. It
    builds its own compliance context from the same raw dict, so a policy it cannot trust is an
    error, not a silently coerced 'this step is unreachable' advisory."""
    config = tmp_path / "openreading.yaml"
    config.write_text("version: 1\npolicy: {require_locall: true}\nstrategies:\n  s: [reducto]\n")
    rc = main(["strategy", "validate", "--config", str(config)])
    assert rc == 3
    err = capsys.readouterr().err
    assert "policy" in err
    assert "did you mean 'require_local'" in err


def test_cli_parse_refuses_a_malformed_file_policy(sample_pdf, tmp_path, capsys):
    """`parse --strategy` reaches the same compile boundary through `api.run`, and lands on the
    same rung as a malformed `--policy` file: the caller should not have to know which of the two
    files carried the bad key."""
    config = tmp_path / "openreading.yaml"
    config.write_text("version: 1\npolicy: {require_locall: true}\nstrategies:\n  s: [pymupdf]\n")
    rc = main(["parse", sample_pdf, "--strategy", "s", "--config", str(config)])
    assert rc == 3
    err = capsys.readouterr().err
    assert "invalid policy: " in err
    assert "Traceback" not in err


def test_server_refuses_a_malformed_config_policy_block_at_startup(tmp_path, monkeypatch):
    """The server reads the OPERATOR's `openreading.yaml`, never a caller-supplied policy, so a
    malformed `policy:` block there is a misconfiguration. It is refused when the file is read,
    which is at startup, rather than on the first request that happens to engage a strategy: an
    operator learns the file is wrong from the process that will not start, not from one caller's
    500. What it must never be is a 200 whose run quietly widened the eligible set.
    """
    pytest.importorskip("fastapi", reason="server extra not installed")
    from openreading.config import ConfigError
    from openreading.server import create_app

    config = tmp_path / "openreading.yaml"
    config.write_text(
        'version: 1\npolicy: {no_train_on_data: true, allow_unverified_compliance: "false"}\n'
        "strategies:\n  s: [nuextract]\n"
    )
    monkeypatch.setenv("OPENREADING_CONFIG", str(config))
    with pytest.raises(ConfigError) as exc:
        create_app()
    assert "allow_unverified_compliance must be true or false" in str(exc.value)
