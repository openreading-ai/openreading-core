"""Resume uses bounded admission and durable protocol jobs without exposing ledger paths."""

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from openreading.mcp_server.general import create_server
from openreading.schemas import execution_tool_schema
from tests.test_execution_jobs import jobs
from tests.test_execution_jobs import store as store
from tests.test_mcp_general import payload, stdio_parameters
from tests.test_mcp_results import reconstruct
from tests.test_mcp_resume import previous


@pytest.mark.asyncio
async def test_stdio_resume_after_restart_delivers_full_retained_response(store, tmp_path):
    from openreading.mcp_server.execution_jobs import slot

    manager, done, _ = previous(store)
    params = stdio_parameters(store, tmp_path)
    with slot(manager.root, 0):
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            accepted = await payload(session, "openreading_resume", {"job_id": done.job_id})
            assert accepted["state"] == "queued"
            assert accepted["receipt"] is None
            Draft202012Validator(execution_tool_schema()).validate(accepted)
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        listing = await payload(session, "openreading_list_jobs", {})
        assert accepted["job_id"] in [row["job_id"] for row in listing["jobs"]]
        result = await payload(
            session, "openreading_get_job", {"job_id": accepted["job_id"], "wait_seconds": 20}
        )
        assert result["state"] == "succeeded", result
        identifier = result["receipt"]["result_id"]
        inline = await payload(session, "openreading_get_result", {"result_id": identifier})
        exported = await payload(
            session, "openreading_get_result", {"result_id": identifier, "delivery": "file"}
        )
        raw = Path(exported["local_path"]).read_bytes()
        assert len(raw) == exported["content_bytes"]
        assert hashlib.sha256(raw).hexdigest() == exported["content_sha256"]
        assert json.loads(raw) == inline["content"]
        assert "OpenReading Test Document" in inline["content"]["payload"]["document"]["text"]
        pages, cursor = [], None
        while True:
            page = await payload(
                session,
                "openreading_get_result",
                {"result_id": identifier, "delivery": "fragments", "cursor": cursor},
            )
            pages.append(page)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert reconstruct(pages) == inline["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"job_id": "/private/secret"},
        {"job_id": "ej1_" + "a" * 32, "ledger_root": "/private/secret"},
        {"job_id": "ej1_" + "a" * 32, "item_index": True},
    ],
)
async def test_resume_protocol_rejects_paths_and_unknown_arguments(store, bad):
    async with create_connected_server_and_client_session(create_server(jobs(store))) as session:
        with pytest.raises(McpError) as caught:
            await session.call_tool("openreading_resume", bad)
        assert "secret" not in str(caught.value)


def test_resume_preflights_acceptance_before_job_lookup(store, monkeypatch):
    from openreading.artifacts.limits import ArtifactError
    from openreading.mcp_server.general import dispatch

    manager = jobs(store)
    monkeypatch.setattr(
        manager, "start_resume", lambda *_: pytest.fail("resume before budget check")
    )
    with pytest.raises(ArtifactError, match="response_too_large"):
        dispatch(
            manager,
            "openreading_resume",
            {"job_id": "ej1_" + "a" * 32},
            budget=4096,
            request_id="x" * 10000,
        )
