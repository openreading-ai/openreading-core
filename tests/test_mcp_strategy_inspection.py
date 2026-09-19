"""Strategy inspection exposes scoped mechanics without executing or disclosing payloads."""

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
from tests.test_execution_jobs import jobs
from tests.test_execution_jobs import store as store


def authority():
    return ExecutionConfig.from_operator(
        config={
            "version": 1,
            "strategies": {
                "main": {"steps": [{"use": "helper"}, {"backend": "tesseract"}]},
                "helper": {"backend": "pymupdf", "with": {"features": {"prompt": "SECRET"}}},
                "hidden": {"backend": "pymupdf"},
            },
        },
        allowed_backends=["pymupdf"],
        allowed_strategies=["main"],
    )


def test_inspection_module_exists():
    from openreading.mcp_server.strategy_inspection import inspect_strategy

    assert callable(inspect_strategy)


def test_list_and_normalized_views_preserve_scope_and_disclose_omissions():
    from openreading.mcp_server.strategy_inspection import inspect_strategy

    config = authority()
    assert inspect_strategy(config, {"operation": "list"})["strategies"] == ["main"]
    for operation in ("show", "normalize", "plan"):
        result = inspect_strategy(config, {"operation": operation, "strategy": "main"})
        assert result["strategy"] == "main"
        assert result["dispatchable"] == ["pymupdf"]
        assert result["trees"]["helper"] == {"backend": "pymupdf"}
        assert result["omitted"]
        assert not any(s in json.dumps(result) for s in ("SECRET", "hidden", "tesseract"))
        assert result["scope"] == "authorized_strategy_view"
        assert result["configuration_verified"] is False
    assert inspect_strategy(config, {"operation": "show", "strategy": "main"}) == inspect_strategy(
        config, {"operation": "normalize", "strategy": "main"}
    )


@pytest.mark.parametrize("operation", ["show", "normalize", "validate", "plan"])
def test_unauthorized_strategy_refuses_before_normalization(monkeypatch, operation):
    from openreading.mcp_server import strategy_inspection as module

    config = authority()
    monkeypatch.setattr(module, "normalize_strategy", lambda *_: pytest.fail("unscoped read"))
    with pytest.raises(ExecutionRefused, match="scope_denied"):
        module.inspect_strategy(config, {"operation": operation, "strategy": "helper"})


def test_plan_matches_execution_and_does_not_read_source_or_ambient_config(store, monkeypatch):
    from openreading.mcp_server.general import dispatch

    manager = jobs(store)
    monkeypatch.setenv("OPENREADING_CONFIG", "/not/an/authorized/config")
    monkeypatch.setattr(store, "source", lambda *_: pytest.fail("source read"))
    req = {"document": {"path": "missing.pdf"}, "backend": {"id": "strategy:local"}}
    result = dispatch(
        manager,
        "openreading_strategy",
        {"operation": "plan", "strategy": "local", "request": req},
        budget=100_000,
        request_id=1,
    )
    payload = json.loads(result.content[0].text)
    assert payload["dispatchable"] == list(manager.authority.authorize(req).backends)
    assert not list(manager.root.glob("ej1_*"))


def test_validation_is_scoped_and_diagnostics_do_not_echo_config():
    from openreading.mcp_server.strategy_inspection import inspect_strategy

    config = ExecutionConfig.from_operator(
        config={
            "version": 1,
            "strategies": {"ok": {"backend": "pymupdf"}, "bad": {"backend": "missing-secret"}},
        },
        allowed_backends=["pymupdf"],
        allowed_strategies=["ok", "bad"],
    )
    good = inspect_strategy(config, {"operation": "validate", "strategy": "ok"})
    bad = inspect_strategy(config, {"operation": "validate", "strategy": "bad"})
    assert good["valid"] and good["error_count"] == 0
    assert not bad["valid"] and bad["error_count"] > 0
    assert "missing-secret" not in json.dumps(bad)


@pytest.mark.asyncio
async def test_strategy_wire_schema_annotations_errors_and_budgets(store):
    from jsonschema import Draft202012Validator
    from mcp.shared.exceptions import McpError

    from openreading.mcp_server.general import create_server
    from openreading.schemas import strategy_tool_schema
    from openreading.types.strategy_tool import strategy_tool_contract

    assert strategy_tool_schema() == strategy_tool_contract()
    server = create_server(jobs(store), document_response_bytes=4096)
    async with create_connected_server_and_client_session(server) as session:
        tool = next(
            t for t in (await session.list_tools()).tools if t.name == "openreading_strategy"
        )
        assert tool.annotations.readOnlyHint and tool.annotations.idempotentHint
        assert not tool.annotations.openWorldHint
        for operation in ("show", "normalize", "validate", "plan"):
            reply = await session.call_tool(
                tool.name, {"operation": operation, "strategy": "local"}
            )
            assert not reply.isError
            Draft202012Validator(strategy_tool_schema()).validate(json.loads(reply.content[0].text))
        for value in (
            {"operation": "list", "strategy": "local"},
            {"operation": "show"},
            {"operation": "validate", "strategy": "local", "request": {}},
            {"operation": "show", "strategy": "local", "config": "SECRET"},
        ):
            with pytest.raises(McpError) as error:
                await session.call_tool(tool.name, value)
            assert "SECRET" not in str(error.value)


def test_oversized_strategy_reply_refuses_without_truncation(store):
    from openreading.artifacts.limits import ArtifactError
    from openreading.mcp_server.general import dispatch

    with pytest.raises(ArtifactError, match="response_too_large"):
        dispatch(
            jobs(store),
            "openreading_strategy",
            {"operation": "list"},
            budget=64,
            request_id='"界' * 80,
        )


def test_tree_view_omits_string_arrays_and_payload_annotations():
    from openreading.mcp_server.strategy_inspection import _view

    view, count = _view(
        {
            "backend": "pymupdf",
            "with": {"key": "SECRET"},
            "intent": "SECRET",
            "escalate_if": {"missing_fields": ["SECRET"], "min_text_chars": 12},
        }
    )
    assert "SECRET" not in json.dumps(view)
    assert view["backend"] == "pymupdf"
    assert count >= 3


@pytest.mark.parametrize("operation", ["list", "plan", "validate"])
def test_strategy_operation_fields_are_enforced_in_wire_schema(operation):
    from jsonschema import Draft202012Validator, ValidationError

    from openreading.mcp_server.general import INPUTS
    from openreading.schemas import strategy_tool_schema

    invalid = {"operation": operation}
    if operation == "list":
        invalid["strategy"] = "secret"
    with pytest.raises(ValidationError):
        Draft202012Validator(INPUTS["openreading_strategy"]).validate(invalid)
    Draft202012Validator.check_schema(strategy_tool_schema())


def test_plan_refuses_different_entrypoint_and_fully_pruned_scope():
    from openreading.mcp_server.strategy_inspection import inspect_strategy

    config = authority()
    with pytest.raises(ExecutionRefused, match="invalid_request"):
        inspect_strategy(
            config,
            {
                "operation": "plan",
                "strategy": "main",
                "request": {
                    "document": {"path": "sample.pdf"},
                    "backend": {"id": "strategy:helper"},
                },
            },
        )
    empty = ExecutionConfig(config.configuration, frozenset(), config.allowed_strategies)
    with pytest.raises(ExecutionRefused, match="scope_denied"):
        inspect_strategy(empty, {"operation": "plan", "strategy": "main"})


@pytest.mark.asyncio
async def test_invalid_strategy_validation_is_error_reply(store):
    from openreading.mcp_server.execution_jobs import ExecutionJobs
    from openreading.mcp_server.general import create_server

    config = ExecutionConfig.from_operator(
        config={"version": 1, "strategies": {"bad": {"backend": "unknown-secret"}}},
        allowed_strategies=["bad"],
    )
    async with create_connected_server_and_client_session(
        create_server(ExecutionJobs(store, config))
    ) as session:
        reply = await session.call_tool(
            "openreading_strategy", {"operation": "validate", "strategy": "bad"}
        )
        assert reply.isError
        assert not json.loads(reply.content[0].text)["valid"]
        assert "unknown-secret" not in reply.content[0].text


def test_validation_keeps_execution_defaults_while_excluding_other_entrypoints():
    from openreading.mcp_server.strategy_inspection import inspect_strategy

    config = ExecutionConfig.from_operator(
        config={
            "version": 1,
            "defaults": {"advanced": {"attempt_timeout": "1s"}},
            "strategies": {"local": {"backend": "pymupdf"}},
        },
        allowed_backends=["pymupdf"],
        allowed_strategies=["local"],
    )
    result = inspect_strategy(config, {"operation": "validate", "strategy": "local"})
    assert not result["valid"]
    assert result["error_count"] >= 1


def test_validation_preserves_plain_dialect_warnings():
    from openreading.mcp_server.strategy_inspection import inspect_strategy
    from openreading.strategies.validate import validate_config

    config = ExecutionConfig.from_operator(
        config={
            "version": 1,
            "strategies": {
                "main": {"try": ["helper", "pymupdf"], "escalate_when": {"looks_bad": True}},
                "helper": {"backend": "pymupdf"},
            },
        },
        allowed_backends=["pymupdf"],
        allowed_strategies=["main"],
    )
    loaded = config.loaded
    issues = validate_config(loaded.config, raw=loaded.raw, plain_info=loaded.plain_info)
    expected = sum(issue.level == "warning" for issue in issues)
    assert expected > 0
    result = inspect_strategy(config, {"operation": "validate", "strategy": "main"})
    assert result["warning_count"] == expected


@pytest.mark.parametrize("operation", ["show", "normalize", "plan"])
def test_judge_payload_is_omitted_from_strategy_views(operation):
    from openreading.mcp_server.strategy_inspection import inspect_strategy

    config = ExecutionConfig.from_operator(
        config={
            "version": 1,
            "strategies": {
                "main": {
                    "parallel": [{"backend": "pymupdf"}, {"backend": "tesseract"}],
                    "pick": "best",
                    "judge": {
                        "backend": "tesseract",
                        "intent": "PRIVATE_JUDGE_INSTRUCTIONS",
                        "excerpt_chars": 4000,
                    },
                }
            },
        },
        allowed_backends=["pymupdf", "tesseract"],
        allowed_strategies=["main"],
    )
    result = inspect_strategy(config, {"operation": operation, "strategy": "main"})
    expected = {
        "parallel": [{"backend": "pymupdf"}, {"backend": "tesseract"}],
        "pick": "best",
    }
    assert result["tree"] == expected
    assert result["trees"] == {"main": expected}
    assert "PRIVATE_JUDGE_INSTRUCTIONS" not in json.dumps(result)
    assert result["omitted"] == 1


@pytest.mark.parametrize("operation", ["show", "normalize", "plan"])
def test_dispatchable_excludes_authorized_backends_absent_from_strategy(operation):
    from openreading.mcp_server.strategy_inspection import inspect_strategy

    config = ExecutionConfig.from_operator(
        config={"version": 1, "strategies": {"main": {"backend": "pymupdf"}}},
        allowed_backends=["pymupdf", "tesseract"],
        allowed_strategies=["main"],
    )
    result = inspect_strategy(config, {"operation": operation, "strategy": "main"})
    assert result["dispatchable"] == ["pymupdf"]
