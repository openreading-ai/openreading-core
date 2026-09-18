"""General MCP execution remains scoped, bounded and independent of a local import engine."""

import importlib.util
import json

import pytest
from jsonschema import Draft202012Validator
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from tests.test_execution_jobs import jobs, request
from tests.test_execution_jobs import store as store


def test_general_tools_exist():
    assert importlib.util.find_spec("openreading.mcp_server.general") is not None


@pytest.mark.asyncio
async def test_general_catalog_contract_scope_and_annotations(store, monkeypatch):
    from openreading.mcp_server.general import create_server
    from openreading.schemas import execution_tool_schema

    manager = jobs(store)
    monkeypatch.setattr(store, "source", lambda *_: pytest.fail("unscoped acquisition"))
    async with create_connected_server_and_client_session(create_server(manager)) as session:
        catalog = {t.name: t for t in (await session.list_tools()).tools}
        assert set(catalog) == {
            "openreading_parse",
            "openreading_batch",
            "openreading_resume",
            "openreading_get_job",
            "openreading_list_jobs",
            "openreading_cancel_job",
            "openreading_get_result",
            "openreading_compare",
            "openreading_route",
        }
        for name in [
            "openreading_get_job",
            "openreading_list_jobs",
            "openreading_cancel_job",
            "openreading_parse",
        ]:
            assert not catalog[name].annotations.readOnlyHint
        assert catalog["openreading_parse"].annotations.openWorldHint
        assert not catalog["openreading_parse"].annotations.idempotentHint
        assert catalog["openreading_resume"].annotations.openWorldHint
        assert not catalog["openreading_resume"].annotations.idempotentHint
        assert not catalog["openreading_resume"].annotations.readOnlyHint
        assert all(
            not tool.annotations.openWorldHint
            for name, tool in catalog.items()
            if name not in {"openreading_parse", "openreading_batch", "openreading_resume"}
        )
        response = await session.call_tool("openreading_parse", request("reducto"))
        assert response.isError
        payload = json.loads(response.content[0].text)
        assert payload["error"]["code"] == "scope_denied"
        Draft202012Validator(execution_tool_schema()).validate(payload)
        assert not list(manager.root.glob("ej1_*"))
        planned = await session.call_tool("openreading_route", {"backend": "reducto"})
        assert json.loads(planned.content[0].text)["chain"] == []
        assert planned.isError
        for arguments in [
            {**request(), "arbitrary": "secret"},
            {"document": {"url": "https://secret.invalid"}, "backend": {}},
            {"document": {"path": "sample.pdf", "password": "secret"}, "backend": {}},
            {"document": {"path": "sample.pdf"}, "backend": {"credentials_ref": "secret"}},
        ]:
            with pytest.raises(McpError) as error:
                await session.call_tool("openreading_parse", arguments)
            assert error.value.error.code == -32602
            assert "secret" not in str(error.value)
        with pytest.raises(McpError) as error:
            await session.call_tool("unknown_secret", {})
        assert error.value.error.code == -32601
        assert "unknown_secret" not in str(error.value)


def test_parse_preflights_serialized_acceptance_before_start(store, monkeypatch):
    from openreading.artifacts.limits import ArtifactError
    from openreading.mcp_server.general import dispatch

    manager = jobs(store)
    monkeypatch.setattr(manager, "start", lambda *_: pytest.fail("started undeliverable job"))
    with pytest.raises(ArtifactError, match="response_too_large"):
        dispatch(manager, "openreading_parse", request(), budget=4096, request_id='"界\\' * 2000)
    assert not list(manager.root.glob("ej1_*"))


def test_cancel_preflights_before_marking_or_recovery(store, monkeypatch):
    from openreading.artifacts.limits import ArtifactError
    from openreading.mcp_server.general import dispatch

    manager = jobs(store)
    for name in ["cancel", "get"]:
        monkeypatch.setattr(
            manager, name, lambda *_a, **_kw: pytest.fail("undeliverable cancellation")
        )
    with pytest.raises(ArtifactError, match="response_too_large"):
        dispatch(
            manager,
            "openreading_cancel_job",
            {"job_id": "ej1_" + "a" * 32},
            budget=4096,
            request_id="x" * 5000,
        )


@pytest.mark.asyncio
async def test_general_recovery_error_and_bounded_listing(store, monkeypatch):
    from openreading.mcp_server.general import create_server
    from tests.test_execution_jobs import prepared

    manager = jobs(store)
    from openreading.mcp_server.execution_jobs import _read_bound, _write_bound

    template = prepared(manager, "0")
    for index in range(1, 50):
        root = manager.root / ("ej1_" + f"{index:032x}")
        root.mkdir()
        for name in ("request.json", "process.json", "status.json"):
            payload = _read_bound(template, name, store.grant)
            if name == "status.json":
                payload["job_id"] = root.name
            _write_bound(root, name, store.grant, payload)
    async with create_connected_server_and_client_session(
        create_server(manager, document_response_bytes=4096)
    ) as session:
        too_large = await session.call_tool("openreading_list_jobs", {"limit": 50})
        assert too_large.isError
        assert json.loads(too_large.content[0].text)["error"]["code"] == "response_too_large"
        seen, cursor = [], None
        while True:
            result = await session.call_tool(
                "openreading_list_jobs", {"limit": 2, "cursor": cursor}
            )
            assert not result.isError
            payload = json.loads(result.content[0].text)
            seen.extend(j["job_id"] for j in payload["jobs"])
            cursor = payload["next_cursor"]
            if cursor is None:
                break
        assert len(set(seen)) == 50
        assert (
            await session.call_tool("openreading_get_job", {"job_id": "ej1_" + "f" * 32})
        ).isError

        def bad(*args, **kwargs):
            raise RuntimeError("secret provider text")

        monkeypatch.setattr(manager, "get", bad)
        failure = await session.call_tool("openreading_get_job", {"job_id": seen[0]})
        assert failure.isError
        assert "secret" not in failure.content[0].text
        assert json.loads(failure.content[0].text)["error"]["code"] == "execution_failed"


def test_general_contract_matches_vendored_and_restricts_shared_request():
    from openreading.mcp_server.general import INPUTS
    from openreading.schemas import execution_tool_schema, request_schema
    from openreading.types.execution_tool import execution_tool_contract

    assert execution_tool_schema() == execution_tool_contract()
    Draft202012Validator.check_schema(execution_tool_schema())
    validator = Draft202012Validator(INPUTS["openreading_parse"])
    validator.validate(request())
    validator.validate(request("strategy:local"))
    assert "url" in request_schema()["properties"]["document"]["properties"]
    for extra in [{"async": {"mode": "async"}}, {"idempotency_key": "private"}]:
        assert not validator.is_valid({**request(), **extra})
    assert INPUTS["openreading_get_job"]["properties"]["wait_seconds"]["maximum"] == 20


def stdio_parameters(store, tmp_path):
    import sys

    from mcp import StdioServerParameters

    config = tmp_path / "operator.json"
    config.write_text(json.dumps({"version": 1, "strategies": {"local": {"backend": "pymupdf"}}}))
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-I",
            "-m",
            "openreading.cli",
            "mcp",
            "--profile",
            "general-execution-v1",
            "--input-root",
            str(store.config.input_root),
            "--artifact-root",
            str(store.config.artifact_root),
            "--execution-config",
            str(config),
            "--execute-backend",
            "pymupdf",
            "--execute-strategy",
            "local",
        ],
    )


async def payload(session, name, arguments):
    reply = await session.call_tool(name, arguments)
    assert not reply.isError, reply
    return json.loads(reply.content[0].text)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["pymupdf", "strategy:local"])
async def test_real_stdio_parse_disconnect_reconnect_and_complete_delivery(
    store, tmp_path, backend
):
    import hashlib
    from pathlib import Path

    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    from openreading.artifacts.models import json_bytes
    from openreading.artifacts.results import RetainedResults
    from openreading.mcp_server.execution_jobs import slot
    from openreading.schemas import execution_tool_schema

    params = stdio_parameters(store, tmp_path)
    manager = jobs(store)
    with slot(manager.root, 0):
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            assert len((await session.list_tools()).tools) == 9
            accepted = await payload(session, "openreading_parse", request(backend))
            Draft202012Validator(execution_tool_schema()).validate(accepted)
            assert accepted["state"] == "queued"
        # The first stdio server exited while the supervisor was waiting for this slot.
        assert manager.get(accepted["job_id"]).state == "queued"
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        listing = await payload(session, "openreading_list_jobs", {})
        assert listing["jobs"][0]["job_id"] == accepted["job_id"]
        final = await payload(
            session, "openreading_get_job", {"job_id": accepted["job_id"], "wait_seconds": 20}
        )
        assert final["state"] == "succeeded", final
        assert final["response_state"] == "succeeded"
        Draft202012Validator(execution_tool_schema()).validate(final)
        identifier = final["receipt"]["result_id"]
        auto = await payload(session, "openreading_get_result", {"result_id": identifier})
        assert auto["delivery"] == "tool_result"
        assert "OpenReading Test Document" in auto["content"]["payload"]["document"]["text"]
        file = await payload(
            session, "openreading_get_result", {"result_id": identifier, "delivery": "file"}
        )
        raw = Path(file["local_path"]).read_bytes()
        assert raw == json_bytes(auto["content"])
        assert len(raw) == file["content_bytes"]
        assert hashlib.sha256(raw).hexdigest() == file["content_sha256"]
        assert auto["content"] == RetainedResults(store).load(identifier).wire()
        compared = await payload(
            session, "openreading_compare", {"result_ids": [identifier, identifier]}
        )
        assert compared["kind"] == "comparison_report"
        report = await payload(
            session, "openreading_get_result", {"result_id": compared["result_id"]}
        )
        assert report["content"]["kind"] == "comparison_report"
        stopped = await payload(session, "openreading_cancel_job", {"job_id": accepted["job_id"]})
        assert stopped == final
    assert len(list(manager.root.glob("ej1_*"))) == 1


@pytest.mark.asyncio
async def test_real_stdio_cancel_discovered_queued_job_without_execution(store, tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    from openreading.mcp_server.execution_jobs import slot

    manager = jobs(store)
    params = stdio_parameters(store, tmp_path)
    with slot(manager.root, 0):
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            accepted = await payload(session, "openreading_parse", request())
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            listing = await payload(session, "openreading_list_jobs", {})
            identifier = listing["jobs"][0]["job_id"]
            assert identifier == accepted["job_id"]
            await payload(session, "openreading_cancel_job", {"job_id": identifier})
            final = await payload(
                session, "openreading_get_job", {"job_id": identifier, "wait_seconds": 20}
            )
            assert final["state"] == "cancelled"
            assert final["receipt"] is None
            assert final["error"]["code"] == "cancelled"
    assert not list((store.config.artifact_root / "execution").rglob("source.*"))


def test_parse_keeps_non_authority_document_metadata():
    from openreading.mcp_server.general import INPUTS

    value = request()
    value["document"].update(filename="chosen.data", mime_type="application/pdf")
    Draft202012Validator(INPUTS["openreading_parse"]).validate(value)


@pytest.mark.asyncio
async def test_error_result_also_enforces_response_budget(store, monkeypatch):
    from openreading.mcp_server import general

    manager = jobs(store)
    async with create_connected_server_and_client_session(
        general.create_server(manager)
    ) as session:
        monkeypatch.setattr(general, "response_bytes", lambda *_: 1_000_001)
        with pytest.raises(McpError) as error:
            await session.call_tool("openreading_get_job", {"job_id": "ej1_" + "a" * 32})
        assert error.value.error.code == -32603
        assert "exceeds" in error.value.error.message


@pytest.mark.parametrize("policy", [None, {"backends": []}, {"backends": ["pymupdf"]}])
def test_default_general_route_matches_execution_preflight(store, policy):
    from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
    from openreading.mcp_server.execution_jobs import ExecutionJobs
    from openreading.mcp_server.general import dispatch

    config = {"version": 1}
    if policy is not None:
        config["policy"] = policy
    authority = ExecutionConfig.from_operator(config=config, allowed_backends=["pymupdf"])
    try:
        expected = list(
            authority.authorize({"document": {"path": "sample.pdf"}, "backend": {}}).backends
        )
    except ExecutionRefused as error:
        assert error.code == "scope_denied"
        expected = []
    result = dispatch(
        ExecutionJobs(store, authority), "openreading_route", {}, budget=4096, request_id=1
    )
    assert json.loads(result.content[0].text)["chain"] == expected
    assert result.isError is (not expected)


@pytest.mark.asyncio
async def test_strict_job_arguments_use_sanitized_protocol_errors(store):
    from openreading.mcp_server.general import create_server

    async with create_connected_server_and_client_session(create_server(jobs(store))) as session:
        with pytest.raises(McpError) as error:
            await session.call_tool("openreading_list_jobs", {"limit": 1.0})
        assert error.value.error.code == -32602


def test_help_names_and_count_match_general_catalog():
    import re

    from openreading import cli
    from openreading.mcp_server.general import INPUTS

    count = re.search(r"general-execution-v1 for (\d+) tools", cli.__doc__)
    assert count and int(count[1]) == len(INPUTS)
    assert all(name in cli.__doc__ for name in INPUTS)


def test_general_route_uses_router_default(store, monkeypatch):
    from openreading.mcp_server.execution import ExecutionConfig
    from openreading.mcp_server.execution_jobs import ExecutionJobs
    from openreading.mcp_server.general import dispatch
    from openreading.router.router import Router

    monkeypatch.setattr(Router, "DEFAULT_BACKEND", "tesseract")
    authority = ExecutionConfig.from_operator(allowed_backends=["tesseract"])
    expected = list(
        authority.authorize({"document": {"path": "sample.pdf"}, "backend": {}}).backends
    )
    result = dispatch(
        ExecutionJobs(store, authority), "openreading_route", {}, budget=4096, request_id=1
    )
    assert expected == ["tesseract"]
    assert json.loads(result.content[0].text)["chain"] == expected
    assert not result.isError
