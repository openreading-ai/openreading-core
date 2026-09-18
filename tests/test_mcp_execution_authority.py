"""General execution preflight never widens operator scope or reads document content."""

import copy
import json
from dataclasses import FrozenInstanceError

import pytest


def authority(**kwargs):
    from openreading.mcp_server import execution

    return execution.ExecutionConfig.from_operator(**kwargs)


def request(backend=None, **overrides):
    return {"document": {"path": "sample.pdf"}, "backend": {"id": backend}, **overrides}


def refused(config, value, code):
    from openreading.mcp_server.execution import ExecutionRefused

    with pytest.raises(ExecutionRefused, match=f"^{code}$") as error:
        config.authorize(value)
    assert error.value.code == code
    assert "secret" not in str(error.value)


def test_authority_is_available_without_changing_tool_catalog():
    import importlib.util

    from openreading.mcp_server.tools import INPUTS

    assert importlib.util.find_spec("openreading.mcp_server.execution") is not None
    assert len(INPUTS) == 13
    assert "openreading_parse" not in INPUTS


def test_explicit_snapshot_is_deep_and_ignores_ambient_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "openreading.yaml").write_text("broken: secret\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(tmp_path / "missing-secret.yaml"))
    raw = {"version": 1, "policy": {"backends": ["reducto", "tesseract", "pymupdf"]}}
    allowed = ["pymupdf", "tesseract"]
    config = authority(config=raw, allowed_backends=allowed)
    fingerprint = config.fingerprint
    raw["policy"]["backends"].clear()
    allowed.append("reducto")
    loaded = config.loaded
    loaded.config.policy.backends.append("reducto")
    loaded.raw["policy"]["backends"].clear()
    assert config.authorize(request()).backends == ("tesseract", "pymupdf")
    assert config.fingerprint == fingerprint
    assert authority(allowed_backends=["pymupdf"]).authorize(request()).backends == ("pymupdf",)
    with pytest.raises(FrozenInstanceError):
        config.allowed_backends = frozenset({"reducto"})

    path = tmp_path / "explicit.yaml"
    path.write_text("version: 1\npolicy:\n  backends: [tesseract, pymupdf]\n")
    snap = authority(config=path, allowed_backends=["tesseract", "pymupdf"])
    path.unlink()
    assert snap.authorize(request()).backends == ("tesseract", "pymupdf")


def test_fingerprint_binds_configuration_and_independent_scopes():
    raw = {"version": 1, "policy": {"backends": ["pymupdf"]}}
    first = authority(config=raw, allowed_backends=["pymupdf", "tesseract"])
    assert (
        first.fingerprint
        == authority(
            config={"policy": {"backends": ["pymupdf"]}, "version": 1},
            allowed_backends=["tesseract", "pymupdf", "tesseract"],
        ).fingerprint
    )
    assert first.fingerprint != authority(config=raw, allowed_backends=["pymupdf"]).fingerprint
    assert (
        first.fingerprint
        != authority(
            config=raw, allowed_backends=["pymupdf", "tesseract"], allowed_strategies=["fast"]
        ).fingerprint
    )
    assert (
        first.fingerprint
        != authority(
            config={"version": 1, "policy": {"backends": ["tesseract"]}},
            allowed_backends=["pymupdf", "tesseract"],
        ).fingerprint
    )


def test_policy_and_fallback_do_not_grant_backends():
    config = authority(
        config={"version": 1, "policy": {"backends": ["reducto", "tesseract", "pymupdf"]}},
        allowed_backends=["pymupdf", "tesseract"],
    )
    assert config.authorize(request(routing={"fallback": ["pymupdf", "reducto"]})).backends == (
        "pymupdf",
        "tesseract",
    )
    refused(config, request("reducto"), "scope_denied")
    refused(authority(), request(), "scope_denied")
    refused(authority(allowed_backends=[]), request("pymupdf"), "scope_denied")
    refused(
        authority(config={"version": 1, "policy": {"backends": ["reducto"]}}),
        request(),
        "scope_denied",
    )
    # A named authorized backend does not need membership in the default chain.
    outside_default = authority(
        config={"version": 1, "policy": {"backends": []}}, allowed_backends=["pymupdf"]
    )
    assert outside_default.authorize(request("pymupdf")).backends == ("pymupdf",)
    refused(outside_default, request(), "scope_denied")


def test_named_precheck_precedes_registry_and_credentials(monkeypatch):
    from openreading.mcp_server import execution

    def forbidden(*args, **kwargs):
        raise AssertionError("Denied request reached registry")

    config = authority(allowed_backends=["pymupdf"])
    monkeypatch.setattr(execution, "build_registry", forbidden)
    refused(config, request("reducto"), "scope_denied")
    refused(config, request("unknown-secret"), "scope_denied")
    refused(config, request("strategy:secret"), "scope_denied")


@pytest.mark.parametrize(
    "value",
    [
        {"document": {"url": "https://secret.invalid/x"}, "backend": {}},
        {"document": {"bytes_base64": "c2VjcmV0"}, "backend": {}},
        {"document": {"file_id": "secret"}, "backend": {}},
        request(backend="pymupdf", document={"path": "sample.pdf", "password": "secret"}),
        {**request(), "backend": {"credentials_ref": "env:SECRET"}},
        {**request(), "backend": {"runtime": {"endpoint": "https://secret.invalid"}}},
        {**request(), "backend": {"runtime": {}}},
        request(**{"async": {"webhook_url": "https://secret.invalid"}}),
        request(**{"async": {"mode": "async"}}),
        request(idempotency_key="secret"),
        request(config="secret"),
        request(allowed_backends=["secret"]),
        request(schema_version="secret"),
        request(features={"layout": "false"}),
        request(document={"path": "/secret.pdf"}),
        request(document={"path": "../secret.pdf"}),
        request(document={"path": "dir/../secret.pdf"}),
        request(document={"path": "dir//secret.pdf"}),
        request(document={"path": "dir/./secret.pdf"}),
        request(document={"path": "secret\x00.pdf"}),
        request(document={"path": ""}),
    ],
)
def test_requests_cannot_supply_authority_or_acquisition_controls(value):
    refused(authority(allowed_backends=["pymupdf"]), value, "invalid_request")


def test_request_snapshot_preserves_processing_options_without_format_filter():
    value = request(
        document={
            "path": "nested/café.custom",
            "filename": "café.custom",
            "mime_type": "text/plain",
        },
        features={"ocr": "off", "layout": False},
        outputs={"tables": "cells", "include_backend_raw": True},
        pages={"ranges": [{"start": 2, "end": 4}]},
        extraction_schema={"json_schema": {"type": "object"}, "instructions": "literal content"},
    )
    original = copy.deepcopy(value)
    plan = authority(allowed_backends=["pymupdf"]).authorize(value)
    assert value == original
    value["features"]["ocr"] = "force"
    first = plan.request
    first.document.path = "/secret.pdf"
    first.features.ocr = "force"
    assert plan.request.document.path == "nested/café.custom"
    assert plan.request.features.ocr == "off"
    assert plan.request.outputs.tables == "cells"
    assert plan.request.pages.ranges[0].end == 4
    assert plan.request.extraction_schema.instructions == "literal content"
    assert json.loads(plan.request_json)["document"]["filename"] == "café.custom"


def strategy_config(default=False):
    raw = {
        "version": 1,
        "policy": {"backends": ["reducto"]},
        "strategies": {
            "entry": {"steps": [{"use": "helper"}, {"backend": "reducto"}]},
            "helper": {"backend": "pymupdf"},
        },
    }
    if default:
        raw["defaults"] = {"strategy": "entry"}
    return raw


@pytest.mark.parametrize("default", [False, True])
def test_strategy_entrypoint_and_nested_backends_have_separate_scopes(default):
    value = request(None if default else "strategy:entry")
    raw = strategy_config(default)
    blocked = authority(config=raw, allowed_backends=["pymupdf"])
    refused(blocked, value, "scope_denied")
    config = authority(config=raw, allowed_backends=["pymupdf"], allowed_strategies=["entry"])
    plan = config.authorize(value)
    assert plan.strategy == "entry"
    assert plan.backends == ("pymupdf",)
    refused(config, request("strategy:helper"), "scope_denied")
    refused(authority(config=raw, allowed_strategies=["entry"]), value, "scope_denied")
    # Granting an entrypoint grants its configured helper tree, never an extra backend.
    config2 = authority(config=raw, allowed_backends=["reducto"], allowed_strategies=["entry"])
    assert config2.authorize(value).backends == ("reducto",)


def test_none_escape_and_presets_need_no_implicit_strategy_grant():
    raw = strategy_config(True)
    raw["policy"] = {"backends": ["pymupdf"]}
    config = authority(config=raw, allowed_backends=["pymupdf"])
    refused(config, request(), "scope_denied")
    assert config.authorize(request("strategy:none")).strategy is None
    assert config.authorize(request("strategy:none")).backends == ("pymupdf",)
    refused(config, request("strategy:fast"), "scope_denied")
    preset = authority(allowed_backends=["pymupdf"], allowed_strategies=["fast"])
    assert preset.authorize(request("strategy:fast")).backends == ("pymupdf",)


def test_preflight_never_reads_sources_credentials_or_executes(monkeypatch, tmp_path):
    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from openreading.artifacts.store import Store
    from openreading.credentials import EnvCredentialBroker

    def forbidden(*args, **kwargs):
        raise AssertionError("Preflight performed IO or execution")

    for factory in BUILTIN_ADAPTERS.values():
        monkeypatch.setattr(factory, "submit", forbidden)
        monkeypatch.setattr(factory, "health", forbidden)
    monkeypatch.setattr(Store, "source", forbidden)
    monkeypatch.setattr(EnvCredentialBroker, "resolve", forbidden)
    monkeypatch.setattr(EnvCredentialBroker, "resolve_config", forbidden)
    plain = authority(allowed_backends=["pymupdf"])
    assert plain.authorize(request(document={"path": "does-not-exist.pdf"})).backends == (
        "pymupdf",
    )
    strategy = authority(
        config=strategy_config(), allowed_backends=["pymupdf"], allowed_strategies=["entry"]
    )
    assert strategy.authorize(request("strategy:entry")).backends == ("pymupdf",)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allowed_backends": ["unknown-secret"]},
        {"allowed_backends": "pymupdf"},
        {"allowed_backends": [1]},
        {"allowed_strategies": "fast"},
        {"allowed_strategies": ["missing-secret"]},
        {"allowed_strategies": ["none"]},
        {"config": {"version": 1, "secret": True}},
        {"config": {"version": 1, "strategies": {"entry": {"backend": "auto"}}}},
    ],
)
def test_operator_setup_errors_are_fixed_and_do_not_echo_values(kwargs):
    from openreading.mcp_server.execution import ExecutionRefused

    with pytest.raises(ExecutionRefused, match="^invalid_configuration$"):
        authority(**kwargs)


@pytest.mark.parametrize("configuration", [b"null", b"[]", b"broken", "{}"])
def test_direct_snapshot_construction_cannot_trigger_ambient_discovery(configuration, monkeypatch):
    from openreading.mcp_server import execution

    original = execution.load

    def explicit_only(config, **kwargs):
        assert config is not None, "Snapshot invoked ambient config discovery"
        return original(config, **kwargs)

    monkeypatch.setattr(execution, "load", explicit_only)
    with pytest.raises(execution.ExecutionRefused, match="^invalid_configuration$"):
        execution.ExecutionConfig(configuration, frozenset(), frozenset())


def test_default_none_is_not_the_explicit_strategy_escape():
    config = authority(
        config={"version": 1, "defaults": {"strategy": "none"}},
        allowed_backends=["pymupdf"],
    )
    refused(config, request(), "scope_denied")
    assert config.authorize(request("strategy:none")).backends == ("pymupdf",)


def test_configured_decider_is_reported_only_inside_scope():
    raw = strategy_config()
    raw["decider"] = {"llm": {"backend": "qwen-vl", "send_document_content": False}}
    allowed = authority(
        config=raw, allowed_backends=["pymupdf", "qwen-vl"], allowed_strategies=["entry"]
    )
    assert allowed.authorize(request("strategy:entry")).backends == ("pymupdf", "qwen-vl")
    denied = authority(config=raw, allowed_backends=["pymupdf"], allowed_strategies=["entry"])
    assert denied.authorize(request("strategy:entry")).backends == ("pymupdf",)


def test_bad_normalized_strategy_returns_fixed_error():
    config = authority(
        config={"version": 1, "strategies": {"fast": {"backend": "pymupdf"}}},
        allowed_backends=["pymupdf"],
        allowed_strategies=["fast"],
    )
    refused(config, request("strategy:fast"), "invalid_configuration")


def test_fully_pruned_strategy_refusal_does_not_echo_compiler_details():
    config = authority(
        config={"version": 1, "strategies": {"secret-entry": {"backend": "reducto"}}},
        allowed_backends=["pymupdf"],
        allowed_strategies=["secret-entry"],
    )
    refused(config, request("strategy:secret-entry"), "scope_denied")
