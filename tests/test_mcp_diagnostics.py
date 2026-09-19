"""General backend diagnostics preserve scope, explicit environment and bounded transport."""

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tests.test_execution_jobs import jobs
from tests.test_execution_jobs import store as store


def test_catalog_is_scoped_static_and_preserves_descriptor(store, monkeypatch):
    from openreading.adapters.registry import make_adapter
    from openreading.mcp_server import diagnostics

    manager = jobs(store)
    adapter = make_adapter("pymupdf")
    monkeypatch.setattr(adapter, "health", lambda: pytest.fail("discovery checked readiness"))
    monkeypatch.setattr(diagnostics, "make_adapter", lambda _: adapter)
    result = diagnostics.describe(manager.authority, {})
    assert result["readiness"] == "not_checked"
    assert result["backends"] == [adapter.descriptor.model_dump(mode="json")]
    assert diagnostics.describe(manager.authority, {"backend": "pymupdf"}) == result
    with pytest.raises(Exception, match="scope_denied"):
        diagnostics.describe(manager.authority, {"backend": "reducto"})


def test_worker_scope_precedes_adapter_or_credentials(monkeypatch):
    from openreading.mcp_server import diagnostic_worker
    from openreading.mcp_server.execution import ExecutionRefused

    monkeypatch.setattr(diagnostic_worker, "make_adapter", lambda _: pytest.fail("adapter touched"))
    with pytest.raises(ExecutionRefused, match="scope_denied"):
        diagnostic_worker.execute(
            {"allowed_backends": [], "backend": "pymupdf", "operation": "readiness", "timeout_s": 1}
        )


def test_readiness_uses_shared_offline_engine_with_explicit_environment(monkeypatch):
    from openreading.mcp_server import diagnostic_worker

    monkeypatch.setenv("REDUCTO_API_KEY", "ambient-secret")
    result = diagnostic_worker.execute(
        {
            "allowed_backends": ["reducto"],
            "backend": "reducto",
            "operation": "readiness",
            "timeout_s": 1,
        },
        environ={},
    )
    assert not result["readiness"]["ready"]
    assert "REDUCTO_API_KEY" in result["readiness"]["required_missing"]
    assert "ambient-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_diagnostic_tools_registered_without_changing_local_catalog(store):
    from openreading.mcp_server.general import create_server

    async with create_connected_server_and_client_session(create_server(jobs(store))) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert len(tools) == 13
        assert tools["openreading_backends"].annotations.readOnlyHint
        assert not tools["openreading_readiness"].annotations.readOnlyHint
        assert not tools["openreading_liveness"].annotations.readOnlyHint
        assert not tools["openreading_readiness"].annotations.openWorldHint
        assert tools["openreading_liveness"].annotations.openWorldHint
        reply = await session.call_tool("openreading_backends", {})
        assert not reply.isError
        assert [row["id"] for row in json.loads(reply.content[0].text)["backends"]] == ["pymupdf"]


def test_catalog_empty_scope_and_reply_ceiling(store, monkeypatch):
    from openreading.artifacts.limits import ArtifactError
    from openreading.mcp_server import diagnostics
    from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused

    assert diagnostics.describe(ExecutionConfig.from_operator(), {})["backends"] == []
    monkeypatch.setattr(
        diagnostics, "make_adapter", lambda _: pytest.fail("foreign adapter constructed")
    )
    with pytest.raises(ExecutionRefused, match="scope_denied"):
        diagnostics.describe(jobs(store).authority, {"backend": "reducto"})
    monkeypatch.undo()
    monkeypatch.setattr(diagnostics, "MAX_DIAGNOSTIC_BYTES", 1)
    with pytest.raises(ArtifactError, match="response_too_large"):
        diagnostics.describe(jobs(store).authority, {})


def test_diagnostic_budget_refuses_before_start(store, monkeypatch):
    from openreading.artifacts.limits import ArtifactError
    from openreading.mcp_server import general

    monkeypatch.setattr(
        general.DiagnosticAttempt, "check_backend", lambda *_: pytest.fail("diagnostic started")
    )
    for name in ("openreading_readiness", "openreading_liveness"):
        with pytest.raises(ArtifactError, match="response_too_large"):
            general.dispatch(
                jobs(store), name, {"backend": "pymupdf"}, budget=4096, request_id='"界\\' * 2000
            )


def test_parent_scope_refuses_before_scratch(store, monkeypatch):
    from openreading.mcp_server.diagnostics import DiagnosticAttempt
    from openreading.mcp_server.execution import ExecutionRefused

    manager = jobs(store)
    attempt = DiagnosticAttempt(store, manager.authority)
    monkeypatch.setattr(attempt, "_run_child", lambda *_: pytest.fail("foreign child started"))
    with pytest.raises(ExecutionRefused, match="scope_denied"):
        attempt.check_backend("readiness", {"backend": "reducto"})
    assert not attempt.root.exists()


@pytest.mark.parametrize("operation", ["readiness", "liveness"])
def test_real_isolated_diagnostic_preserves_store_and_never_reads_sources(
    store, monkeypatch, operation
):
    from openreading.mcp_server.diagnostics import DiagnosticAttempt
    from tests.test_mcp_resume import inventory

    manager = jobs(store)
    before = inventory(store.config.artifact_root)
    monkeypatch.setattr(store, "source", lambda *_: pytest.fail("diagnostic acquired source"))
    attempt = DiagnosticAttempt(store, manager.authority)
    result = attempt.check_backend(operation, {"backend": "pymupdf"})
    if operation == "readiness":
        assert result["readiness"]["ready"]
        assert result["readiness"]["slug"] == "pymupdf"
    else:
        assert result["backend"] == "pymupdf"
        assert result["status"] == "live" and result["measured"]
    assert attempt.pid is None
    assert not attempt.root.exists()
    assert inventory(store.config.artifact_root) == before
    with pytest.raises(Exception, match="execution_failed"):
        attempt.check_backend(operation, {"backend": "pymupdf"})


def test_worker_liveness_keeps_shared_ladder_and_redacts_forwarded_values(monkeypatch):
    from openreading.mcp_server import diagnostic_worker
    from openreading.types.liveness import LivenessReport

    calls = []

    def probe(adapter, *, broker, timeout_s):
        calls.append((adapter.descriptor.id, timeout_s))
        return LivenessReport(
            backend="pymupdf",
            status="unreachable",
            measured=True,
            probe="local",
            checked_at="2026-09-18T00:00:00Z",
            latency_ms=3,
            detail="failed secret-value at https://private.invalid",
            version="secret-value",
        )

    monkeypatch.setattr(diagnostic_worker, "check_liveness", probe)
    report = diagnostic_worker.execute(
        {
            "operation": "liveness",
            "backend": "pymupdf",
            "allowed_backends": ["pymupdf"],
            "timeout_s": 0.5,
        },
        environ={"SECRET": "secret-value", "ENDPOINT": "https://private.invalid"},
    )
    assert calls == [("pymupdf", 0.5)]
    assert report["measured"] and report["status"] == "unreachable"
    assert "secret-value" not in json.dumps(report) and "private.invalid" not in json.dumps(report)
    assert report["latency_ms"] == 3


def test_worker_readiness_never_probes(monkeypatch):
    from dataclasses import asdict

    from openreading.adapters.registry import make_adapter
    from openreading.credentials import EnvCredentialBroker
    from openreading.mcp_server import diagnostic_worker
    from openreading.readiness import backend_readiness

    monkeypatch.setattr(
        diagnostic_worker, "check_liveness", lambda *_a, **_kw: pytest.fail("readiness probed")
    )
    adapter = make_adapter("pymupdf")
    monkeypatch.setattr(diagnostic_worker, "make_adapter", lambda _: adapter)
    expected = asdict(backend_readiness(adapter, broker=EnvCredentialBroker(environ={})))
    actual = diagnostic_worker.execute(
        {"operation": "readiness", "backend": "pymupdf", "allowed_backends": ["pymupdf"]},
        environ={},
    )
    assert actual["readiness"] == expected


def test_contract_matches_schema_and_rejects_authority_overrides():
    from jsonschema import Draft202012Validator

    from openreading.mcp_server.general import INPUTS
    from openreading.schemas import diagnostic_tool_schema
    from openreading.types.diagnostic_tool import diagnostic_tool_contract

    assert diagnostic_tool_schema() == diagnostic_tool_contract()
    from openreading.schemas import liveness_report_schema

    shared = {
        key: value
        for key, value in liveness_report_schema().items()
        if key not in {"$id", "$schema"}
    }
    assert diagnostic_tool_schema()["$defs"]["LivenessReport"] == shared
    Draft202012Validator.check_schema(diagnostic_tool_schema())
    for name in ("openreading_readiness", "openreading_liveness"):
        validator = Draft202012Validator(INPUTS[name])
        validator.validate({"backend": "pymupdf"})
        for extra in (
            {"runtime": {"endpoint": "secret"}},
            {"credentials_ref": "secret"},
            {"allowed_backends": ["reducto"]},
            {"document": {"path": "secret"}},
        ):
            assert not validator.is_valid({"backend": "pymupdf", **extra})
    validator = Draft202012Validator(INPUTS["openreading_liveness"])
    for value in (-1, 0, 31, True, "5"):
        assert not validator.is_valid({"backend": "pymupdf", "timeout_s": value})


@pytest.mark.asyncio
async def test_diagnostic_protocol_errors_are_sanitized_and_negative_reports_are_not_failures(
    store, monkeypatch
):
    from mcp.shared.exceptions import McpError

    from openreading.mcp_server import general

    async with create_connected_server_and_client_session(
        general.create_server(jobs(store))
    ) as session:
        for name in ("openreading_backends", "openreading_readiness", "openreading_liveness"):
            response = await session.call_tool(name, {"backend": "reducto"})
            assert (
                response.isError
                and json.loads(response.content[0].text)["error"]["code"] == "scope_denied"
            )
            with pytest.raises(McpError) as error:
                await session.call_tool(name, {"backend": "/secret/path"})
            assert error.value.error.code == -32602 and "secret" not in str(error.value)

        def fail(*_a, **_kw):
            raise RuntimeError("secret provider detail")

        monkeypatch.setattr(general.DiagnosticAttempt, "check_backend", fail)
        response = await session.call_tool("openreading_liveness", {"backend": "pymupdf"})
        assert response.isError and "secret" not in response.content[0].text
        monkeypatch.setattr(
            general.DiagnosticAttempt,
            "check_backend",
            lambda *_: {"backend": "pymupdf", "status": "unreachable", "measured": True},
        )
        response = await session.call_tool("openreading_liveness", {"backend": "pymupdf"})
        assert not response.isError


def test_timeout_reaps_owned_diagnostic_and_cleans_scratch(store, monkeypatch):
    import subprocess
    import sys
    from types import SimpleNamespace

    from openreading.mcp_server import diagnostics, execution_process
    from openreading.mcp_server.execution_process import ExecutionError

    launched = []
    real_popen = subprocess.Popen

    def launch(_command, **kwargs):
        child = real_popen([sys.executable, "-c", "import time; time.sleep(100)"], **kwargs)
        launched.append(child)
        return child

    clock = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(execution_process.subprocess, "Popen", launch)

    def exchange(_process, _packet, observe):
        observe()
        pytest.fail("deadline did not stop diagnostic")

    monkeypatch.setattr(execution_process, "_exchange", exchange)
    attempt = diagnostics.DiagnosticAttempt(store, jobs(store).authority)
    with pytest.raises(ExecutionError, match="timeout"):
        attempt.check_backend("liveness", {"backend": "pymupdf", "timeout_s": 0.1})
    assert len(launched) == 1 and launched[0].poll() is not None
    assert attempt.pid is None and not attempt.root.exists()


def test_child_environment_is_explicit_and_parent_unchanged(store, monkeypatch):
    import os
    import subprocess

    from openreading.mcp_server import diagnostics, execution_process

    monkeypatch.setenv("UNFORWARDED_DIAGNOSTIC_SECRET", "never-pass")
    before = dict(os.environ)
    real_popen = subprocess.Popen
    seen = []

    def launch(command, **kwargs):
        seen.append((command, kwargs["env"]))
        return real_popen(command, **kwargs)

    monkeypatch.setattr(execution_process.subprocess, "Popen", launch)
    attempt = diagnostics.DiagnosticAttempt(
        store, jobs(store).authority, environment={"FORWARDED_KEY": "selected-value"}
    )
    assert attempt.check_backend("readiness", {"backend": "pymupdf"})["readiness"]["ready"]
    assert len(seen) == 1
    command, env = seen[0]
    assert "-I" in command and "openreading.mcp_server.diagnostic_worker" in command
    assert "UNFORWARDED_DIAGNOSTIC_SECRET" not in env
    assert env["FORWARDED_KEY"] == "selected-value"
    assert env["HOME"] == str(attempt.root / "home") and env["OPENREADING_CONFIG"] == ""
    assert dict(os.environ) == before


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"error": {"code": "response_too_large"}}, "response_too_large"),
        (
            {
                "schema_version": "0.1",
                "backend": "reducto",
                "status": "live",
                "measured": True,
                "probe": "local",
                "checked_at": "now",
            },
            "execution_failed",
        ),
    ],
)
def test_parent_refuses_oversized_or_misbound_worker_report(store, monkeypatch, payload, error):
    from openreading.mcp_server.diagnostics import DiagnosticAttempt

    attempt = DiagnosticAttempt(store, jobs(store).authority)
    monkeypatch.setattr(
        attempt,
        "_run_child",
        lambda *_: (attempt.root / "diagnostic.json").write_text(json.dumps(payload)),
    )
    with pytest.raises(Exception, match=error):
        attempt.check_backend("liveness", {"backend": "pymupdf"})
    assert not attempt.root.exists()


@pytest.mark.parametrize("change", ["missing_version", "wrong_version", "missing_probe"])
def test_parent_rejects_liveness_wire_shape_before_model_defaults(store, monkeypatch, change):
    from jsonschema import ValidationError

    from openreading.mcp_server.diagnostics import DiagnosticAttempt
    from openreading.types.liveness import LivenessReport

    payload = {
        "schema_version": "0.1",
        "backend": "pymupdf",
        "status": "live",
        "measured": True,
        "probe": "local",
        "checked_at": "2026-09-18T00:00:00Z",
    }
    if change == "wrong_version":
        payload["schema_version"] = "999"
    else:
        payload.pop("schema_version" if change == "missing_version" else "probe")
    # Model defaults and its unrestricted version string cannot enforce the received wire contract.
    LivenessReport.model_validate(payload)
    attempt = DiagnosticAttempt(store, jobs(store).authority)
    monkeypatch.setattr(
        attempt,
        "_run_child",
        lambda *_: (attempt.root / "diagnostic.json").write_text(json.dumps(payload)),
    )
    with pytest.raises(ValidationError):
        attempt.check_backend("liveness", {"backend": "pymupdf"})
    assert not attempt.root.exists()


def test_worker_main_refuses_oversize_and_sanitizes_exceptions(tmp_path, monkeypatch):
    import io

    from openreading.mcp_server import diagnostic_worker

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(diagnostic_worker, "watch_parent", lambda *_: None)
    monkeypatch.setattr(diagnostic_worker.sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(diagnostic_worker, "execute", lambda *_: {"detail": "x" * 70000})
    assert diagnostic_worker.main(0) == 0
    output = tmp_path / "diagnostic.json"
    assert json.loads(output.read_text()) == {"error": {"code": "response_too_large"}}
    output.unlink()
    monkeypatch.setattr(diagnostic_worker.sys, "stdin", io.StringIO("{}"))

    def fail(*_):
        raise RuntimeError("private provider data")

    monkeypatch.setattr(diagnostic_worker, "execute", fail)
    assert diagnostic_worker.main(0) == 1
    assert not output.exists()


@pytest.mark.asyncio
async def test_real_stdio_general_diagnostics(store, tmp_path):
    from jsonschema import Draft202012Validator
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    from openreading.schemas import diagnostic_tool_schema
    from tests.test_mcp_general import payload, stdio_parameters

    validator = Draft202012Validator(diagnostic_tool_schema())
    async with (
        stdio_client(stdio_parameters(store, tmp_path)) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        for name in ("openreading_backends", "openreading_readiness", "openreading_liveness"):
            result = await payload(session, name, {"backend": "pymupdf"})
            validator.validate(result)
        assert not list(jobs(store).root.glob("ej1_*"))


@pytest.mark.asyncio
async def test_diagnostic_budget_failure_matches_contract(store):
    from jsonschema import Draft202012Validator

    from openreading.mcp_server.general import create_server
    from openreading.schemas import diagnostic_tool_schema

    async with create_connected_server_and_client_session(
        create_server(jobs(store), document_response_bytes=4096)
    ) as session:
        reply = await session.call_tool("openreading_readiness", {"backend": "pymupdf"})
        assert reply.isError
        body = json.loads(reply.content[0].text)
        assert body["error"]["code"] == "response_too_large"
        Draft202012Validator(diagnostic_tool_schema()).validate(body)
