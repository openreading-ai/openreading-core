"""Batch MCP jobs preserve ordered results across disconnects and reject unscoped work."""

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from openreading.artifacts.models import json_bytes
from openreading.artifacts.results import RetainedResults
from tests.test_execution_jobs import jobs, request
from tests.test_execution_jobs import store as store
from tests.test_mcp_general import payload, stdio_parameters
from tests.test_mcp_results import reconstruct


@pytest.mark.asyncio
@pytest.mark.parametrize("backends", [["pymupdf", "strategy:local"], ["pymupdf", "pymupdf"], []])
async def test_stdio_batch_disconnect_retains_every_item_and_complete_delivery(
    store, tmp_path, backends
):
    from openreading.mcp_server.execution_jobs import slot
    from openreading.schemas import execution_tool_schema

    manager = jobs(store)
    params = stdio_parameters(store, tmp_path)
    requests = [request(backend) for backend in backends]
    if "strategy:local" in backends:
        (store.config.input_root / "second.pdf").write_bytes(
            (store.config.input_root / "sample.pdf").read_bytes()
        )
        requests[1]["document"]["path"] = "second.pdf"
    with slot(manager.root, 0):
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            accepted = await payload(session, "openreading_batch", {"requests": requests})
            Draft202012Validator(execution_tool_schema()).validate(accepted)
            assert accepted["state"] == "queued"
            assert accepted["receipt"] is None
        assert manager.get(accepted["job_id"]).state == "queued"
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        listing = await payload(session, "openreading_list_jobs", {})
        assert [job["job_id"] for job in listing["jobs"]] == [accepted["job_id"]]
        final = await payload(
            session, "openreading_get_job", {"job_id": accepted["job_id"], "wait_seconds": 20}
        )
        assert final["state"] == "succeeded", final
        assert final["response_state"] == ("succeeded" if backends else "failed")
        assert final["receipt"]["kind"] == "batch_result"
        Draft202012Validator(execution_tool_schema()).validate(final)
        identifier = final["receipt"]["result_id"]
        inline = await payload(session, "openreading_get_result", {"result_id": identifier})
        assert inline["delivery"] == "tool_result"
        content = inline["content"]
        assert content["kind"] == "batch_result"
        batch = content["payload"]
        assert batch["status"]["state"] == final["response_state"]
        assert batch["summary"]["total"] == len(backends)
        assert batch["summary"]["succeeded"] == len(backends)
        assert batch["summary"]["failed"] == 0
        assert len(batch["items"]) == len(backends)
        assert [item["source"]["relpath"] for item in batch["items"]] == [
            item["document"]["path"] for item in requests
        ]
        for item in batch["items"]:
            assert item["state"] == "succeeded"
            assert "OpenReading Test Document" in item["response"]["document"]["text"]
            assert item["response"]["backend"]["id"] == "pymupdf"
        if not backends:
            assert [warning["code"] for warning in batch["warnings"]] == ["empty_batch"]
        assert content == RetainedResults(store).load(identifier).wire()
        exported = await payload(
            session, "openreading_get_result", {"result_id": identifier, "delivery": "file"}
        )
        raw = Path(exported["local_path"]).read_bytes()
        assert raw == json_bytes(content)
        assert len(raw) == exported["content_bytes"]
        assert hashlib.sha256(raw).hexdigest() == exported["content_sha256"]
        pages, cursor = [], None
        while True:
            page = await payload(
                session,
                "openreading_get_result",
                {"result_id": identifier, "delivery": "fragments", "cursor": cursor},
            )
            assert page["fragment_start"] == sum(len(p["fragments"]) for p in pages)
            pages.append(page)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert reconstruct(pages) == content
        assert (
            await payload(session, "openreading_cancel_job", {"job_id": accepted["job_id"]})
            == final
        )
    assert len(list(manager.root.glob("ej1_*"))) == 1


@pytest.mark.asyncio
async def test_stdio_cancel_discovered_queued_batch_starts_no_attempts(store, tmp_path):
    from openreading.mcp_server.execution_jobs import slot

    manager = jobs(store)
    params = stdio_parameters(store, tmp_path)
    with slot(manager.root, 0):
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            accepted = await payload(
                session,
                "openreading_batch",
                {"requests": [request(), request("strategy:local")]},
            )
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
    assert not list((store.config.artifact_root / "execution").glob("*/*"))
    assert not list(RetainedResults(store).root.glob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("denied_request", "code"),
    [
        (request("private-secret-backend"), "scope_denied"),
        (request("strategy:private-secret"), "scope_denied"),
        ({**request(), "document": {"path": "../private-secret.pdf"}}, "invalid_request"),
        ({**request(), "document": {"path": "/private-secret.pdf"}}, "invalid_request"),
    ],
)
async def test_batch_authorizes_all_requests_before_acquiring_any_source(
    store, monkeypatch, denied_request, code
):
    from openreading.mcp_server.general import create_server
    from openreading.schemas import execution_tool_schema

    manager = jobs(store)
    monkeypatch.setattr(store, "source", lambda *_: pytest.fail("unscoped source acquisition"))
    async with create_connected_server_and_client_session(create_server(manager)) as session:
        denied = await session.call_tool(
            "openreading_batch", {"requests": [request(), denied_request]}
        )
        assert denied.isError
        assert "private-secret" not in denied.content[0].text
        failure = json.loads(denied.content[0].text)
        assert failure["error"]["code"] == code
        Draft202012Validator(execution_tool_schema()).validate(failure)
    assert not list(manager.root.glob("ej1_*"))
    assert not list((store.config.artifact_root / "execution").glob("*/*"))


@pytest.mark.asyncio
async def test_batch_invalid_arguments_are_sanitized_and_create_no_job(store, monkeypatch):
    from openreading.mcp_server.general import create_server

    manager = jobs(store)
    monkeypatch.setattr(store, "source", lambda *_: pytest.fail("invalid source acquisition"))
    invalid = [
        {},
        {"requests": "private-secret"},
        {"requests": [request()], "output": "private-secret"},
        {"requests": [request(), {**request(), "private-secret": True}]},
        {"requests": [{"document": {"url": "https://private-secret.invalid"}}]},
        {"requests": [{"document": {"path": "sample.pdf", "password": "private-secret"}}]},
        {"requests": [{**request(), "idempotency_key": "private-secret"}]},
        {"requests": [{**request(), "async": {"mode": "async"}}]},
        {"requests": [{**request(), "backend": {"credentials_ref": "private-secret"}}]},
    ]
    async with create_connected_server_and_client_session(create_server(manager)) as session:
        for arguments in invalid:
            with pytest.raises(McpError) as error:
                await session.call_tool("openreading_batch", arguments)
            assert error.value.error.code == -32602
            assert "private-secret" not in str(error.value)
    assert not list(manager.root.glob("ej1_*"))


def test_batch_preflights_reply_budget_before_acceptance(store, monkeypatch):
    from openreading.artifacts.limits import ArtifactError
    from openreading.mcp_server.general import dispatch

    manager = jobs(store)
    monkeypatch.setattr(
        manager,
        "start_batch",
        lambda *_: pytest.fail("accepted undeliverable batch"),
        raising=False,
    )
    with pytest.raises(ArtifactError, match="response_too_large"):
        dispatch(
            manager,
            "openreading_batch",
            {"requests": [request()]},
            budget=4096,
            request_id='"界\\' * 2000,
        )
    assert not list(manager.root.glob("ej1_*"))
