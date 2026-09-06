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
from openreading.router.compliance import RouterConfig
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ComplianceRefused

pytest.importorskip("fitz", reason="pymupdf not installed")

from openreading.cli import main  # noqa: E402

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


def test_the_policy_block_and_request_compliance_agree_on_the_five_keys():
    """A key added to `request.compliance` and not to `policy:`, or the reverse, is how the two
    spellings of one constraint start meaning different things. The block is closed now, so the
    schema itself is the enumeration and this test is the only thing tying the two together."""
    policy = schemas.strategy_config_schema()["properties"]["policy"]
    compliance = schemas.request_schema()["properties"]["compliance"]["properties"]
    five = {"require_baa", "no_train_on_data", "data_region", "require_local", "max_retention"}

    assert set(compliance) == five
    for key in sorted(five):
        assert policy["properties"][key]["type"] == compliance[key]["type"], key
        # Copied verbatim, not paraphrased: two descriptions of one key drift the moment one is
        # edited, and the reader has no way to tell which is current.
        assert policy["properties"][key]["description"] == compliance[key]["description"], key


def test_the_policy_block_is_closed_and_holds_exactly_nine_keys():
    policy = schemas.strategy_config_schema()["properties"]["policy"]
    assert policy["additionalProperties"] is False
    assert set(policy["properties"]) == {
        "require_baa",
        "no_train_on_data",
        "data_region",
        "require_local",
        "max_retention",
        "optimize_for",
        "allow_unverified_compliance",
        "train_optout_confirmed",
        "baa_tier_confirmed",
    }
    assert policy["properties"]["optimize_for"]["enum"] == [
        "accuracy",
        "cost",
        "latency",
        "offline",
    ]


def test_doc_type_hint_is_not_a_policy_key(sample_pdf):
    """It stays a request field, and no routing stage reads it. A key that does nothing, in a file
    that gates compliance, is a key a reader will try to rely on."""
    assert "doc_type_hint" in schemas.request_schema()["properties"]["routing"]["properties"]
    assert (
        "doc_type_hint"
        not in schemas.strategy_config_schema()["properties"]["policy"]["properties"]
    )
    with pytest.raises(ConfigError) as exc:
        api.route(sample_pdf, config=_inline({"doc_type_hint": "invoice"}))
    assert "doc_type_hint" in str(exc.value)


# --- symptom 1: unrecognised keys ---------------------------------------------------------------


def test_cli_route_unrecognised_policy_key_exits_3_and_names_the_key(sample_pdf, tmp_path, capsys):
    """`{"hipaa": true, "gdpr": ..., "soc2": ...}` used to route 13 backends with `dropped: {}`."""
    rc = main(
        ["route", sample_pdf, "--config", _policy_block(tmp_path, json.dumps(_OFFICER_POLICY))]
    )
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[route] ")
    for key in _OFFICER_POLICY:
        assert repr(key) in err


def test_python_route_unrecognised_policy_key_raises_config_error(sample_pdf):
    with pytest.raises(ConfigError) as exc:
        api.route(sample_pdf, config=_inline(_OFFICER_POLICY))
    assert "hipaa" in str(exc.value)


def test_misspelled_policy_key_suggests_the_key_that_was_meant(sample_pdf):
    """`require_baaa` is the reported typo: the whole compliance filter vanished without a word.
    The closed schema names the key it did not expect, which is what the reader has to see."""
    with pytest.raises(ConfigError) as exc:
        api.route(sample_pdf, config=_inline({"require_baaa": True}))
    assert "require_baaa" in str(exc.value)
    assert "policy" in str(exc.value)


def test_run_refuses_an_unrecognised_policy_key_before_dispatch(sample_pdf):
    """`run()` reads the file before it builds anything, so the refusal lands before any backend
    is constructed."""
    with pytest.raises(ConfigError):
        api.run(sample_pdf, backend="pymupdf", config=_inline(_OFFICER_POLICY))


def test_run_batch_refuses_an_unrecognised_policy_key(sample_pdf):
    with pytest.raises(ConfigError):
        api.run_batch([sample_pdf], backend="pymupdf", config=_inline(_OFFICER_POLICY))


def test_every_subcommand_that_reads_the_file_refuses_the_same_block(sample_pdf, tmp_path, capsys):
    """The refusal lives where the file is read, not in `cmd_route`, so a subcommand that never
    routes a document refuses the same file under its own tag."""
    path = _policy_block(tmp_path, json.dumps(_OFFICER_POLICY))
    for argv, tag in (
        (["route", sample_pdf, "--config", path], "[route] "),
        (["strategy", "validate", "--config", path], "[strategy validate] "),
    ):
        assert main(argv) == 3, argv
        err = capsys.readouterr().err
        assert "hipaa" in err, argv
        assert tag in err or "hipaa" in err, argv


# --- symptoms 2 and 3: a block that is not an object --------------------------------------------


@pytest.mark.parametrize("body", _NON_OBJECT_BLOCKS)
def test_cli_route_non_object_policy_exits_3_without_a_traceback(
    body, sample_pdf, tmp_path, capsys
):
    """`[]` / `null` used to run with no filter at all; a string crashed with a raw TypeError
    (`_apply_policy`) or AttributeError (`router_config`) depending on its content."""
    rc = main(["route", sample_pdf, "--config", _policy_block(tmp_path, body)])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[route] ")
    assert len(err.splitlines()) == 1
    assert "Traceback" not in err


@pytest.mark.parametrize("policy", ([], "require_baa", "strict", 3, True))
def test_python_route_non_object_policy_raises_config_error(policy, sample_pdf):
    with pytest.raises(ConfigError):
        api.route(sample_pdf, config=_inline(policy))


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


def test_file_policy_unrecognised_key_is_refused_not_dropped(tmp_path):
    """`require_locall` used to be discarded in silence: the run proceeded with NO locality
    constraint at all, and a hosted rung stayed eligible until it died on credentials instead."""
    from openreading import config

    with pytest.raises(config.ConfigError) as exc:
        config.load(_config_file(tmp_path, {"require_locall": True}, "reducto"))
    assert "require_locall" in str(exc.value)


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
    assert "policy/allow_unverified_compliance" in str(exc.value)
    assert "is not of type 'boolean'" in str(exc.value)


def test_file_policy_unquoted_false_keeps_the_unverified_backend_out(sample_pdf, tmp_path):
    """Two spellings of the same policy now agree: the boolean refuses `nuextract` on compliance,
    the string refuses the policy itself. Neither admits it."""
    policy = {"no_train_on_data": True, "allow_unverified_compliance": False}
    path = _config_file(tmp_path, policy, "nuextract")
    with pytest.raises(ComplianceRefused) as exc:
        api.run(sample_pdf, strategy="s", config=path)
    assert "nuextract:trains_unverified" in str(exc.value)


def test_every_reader_of_the_file_gets_the_same_refusal(sample_pdf, tmp_path, capsys):
    """The guard is the schema, and it runs where bytes become a mapping. `calibrate_strategy`
    folds the block by calling `union_compliance` / `merge_router_config` directly, so a guard
    wired into those two would be one a caller could route around. One layer up, no caller can."""
    path = _config_file(tmp_path, {"require_locall": True}, "pymupdf")
    for argv in (
        ["route", sample_pdf, "--config", path],
        ["strategy", "validate", "--config", path],
        ["strategy", "plan", sample_pdf, "--strategy", "s", "--config", path],
        ["parse", sample_pdf, "--strategy", "s", "--config", path],
    ):
        assert main(argv) == 3, argv
        assert "require_locall" in capsys.readouterr().err, argv


def test_the_folds_themselves_no_longer_validate(sample_pdf):
    """The two folds are pure now: they take a block the schema already accepted. Pinned so a
    future edit does not quietly reintroduce a second, drifting validator beside the schema."""
    from openreading.config import merge_router_config, union_compliance

    assert union_compliance(None, {"require_local": True})["require_local"] is True
    assert merge_router_config(
        RouterConfig(), {"baa_tier_confirmed": ["reducto"]}
    ).baa_tier_confirmed == frozenset({"reducto"})


def test_cli_strategy_plan_refuses_a_malformed_file_policy(sample_pdf, tmp_path, capsys):
    """A malformed policy block, end to end: one tagged stderr line and exit 3, the same ladder
    rung any other unloadable config file takes."""
    config = tmp_path / "openreading.yaml"
    config.write_text(
        'version: 1\npolicy: {no_train_on_data: true, allow_unverified_compliance: "false"}\n'
        "strategies:\n  s: [nuextract]\n"
    )
    rc = main(["strategy", "plan", sample_pdf, "--strategy", "s", "--config", str(config)])
    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[strategy plan] ")
    assert "policy/allow_unverified_compliance" in err
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
    assert "invalid config at 'policy'" in err
    assert "require_locall" in err


def test_cli_parse_refuses_a_malformed_file_policy(sample_pdf, tmp_path, capsys):
    """`parse --strategy` reads the file through `api.run` and lands on the same rung: a policy
    that is wrong is wrong before the document is opened, whichever verb opened the file."""
    config = tmp_path / "openreading.yaml"
    config.write_text("version: 1\npolicy: {require_locall: true}\nstrategies:\n  s: [pymupdf]\n")
    rc = main(["parse", sample_pdf, "--strategy", "s", "--config", str(config)])
    assert rc == 3
    err = capsys.readouterr().err
    assert "invalid config at 'policy'" in err
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
    assert "policy/allow_unverified_compliance" in str(exc.value)
    assert "is not of type 'boolean'" in str(exc.value)
