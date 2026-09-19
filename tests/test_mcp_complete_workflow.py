"""Exercise the complete general catalog over real stdio with local processing and restart."""

import hashlib
import json
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from openreading.artifacts.models import json_bytes
from openreading.mcp_server.execution_jobs import slot
from tests.test_execution_jobs import jobs, request
from tests.test_execution_jobs import store as store
from tests.test_mcp_general import stdio_parameters


@pytest.mark.asyncio
async def test_complete_general_workflow_across_stdio_restart(store, tmp_path):
    params = stdio_parameters(store, tmp_path)
    exercised = set()

    async def call(session, name, arguments):
        exercised.add(name)
        reply = await session.call_tool(name, arguments)
        assert not reply.isError, reply
        return json.loads(reply.content[0].text)

    async def finish(session, accepted):
        for _ in range(6):
            state = await call(
                session, "openreading_get_job", {"job_id": accepted["job_id"], "wait_seconds": 20}
            )
            if state["state"] in {"succeeded", "failed", "cancelled"}:
                return state
        pytest.fail("Workflow job did not terminate")

    async def retrieve(session, identifier):
        auto = await call(session, "openreading_get_result", {"result_id": identifier})
        file = await call(
            session, "openreading_get_result", {"result_id": identifier, "delivery": "file"}
        )
        raw = Path(file["local_path"]).read_bytes()
        assert raw == json_bytes(auto["content"])
        assert len(raw) == file["content_bytes"]
        assert hashlib.sha256(raw).hexdigest() == file["content_sha256"] == auto["content_sha256"]
        return auto["content"]

    manager = jobs(store)
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        catalog = {t.name for t in (await session.list_tools()).tools}
        described = await call(session, "openreading_backends", {})
        assert [b["id"] for b in described["backends"]] == ["pymupdf"]
        readiness = await call(session, "openreading_readiness", {"backend": "pymupdf"})
        assert readiness["readiness"]["ready"]
        liveness = await call(session, "openreading_liveness", {"backend": "pymupdf"})
        assert liveness["measured"] and liveness["status"] == "live"
        route = await call(session, "openreading_route", {"backend": "pymupdf"})
        assert route["chain"] == ["pymupdf"]
        for operation in ("list", "show", "normalize", "validate", "plan"):
            arguments = {"operation": operation}
            if operation != "list":
                arguments["strategy"] = "local"
            inspected = await call(session, "openreading_strategy", arguments)
            assert inspected["schema_version"] == "0.1"
        accepted = await call(session, "openreading_parse", request("strategy:local"))
        original = await finish(session, accepted)
        assert original["state"] == "succeeded"
        content = await retrieve(session, original["receipt"]["result_id"])
        assert "OpenReading Test Document" in content["payload"]["document"]["text"]
        assert content["payload"]["orchestration"]["strategy"] == "local"

    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        listing = await call(session, "openreading_list_jobs", {})
        assert original["job_id"] in [row["job_id"] for row in listing["jobs"]]
        resumed = await call(session, "openreading_resume", {"job_id": original["job_id"]})
        resumed = await finish(session, resumed)
        assert resumed["state"] == "succeeded"
        response = await retrieve(session, resumed["receipt"]["result_id"])
        assert response["payload"]["document"]["text"] == content["payload"]["document"]["text"]
        compared = await call(
            session,
            "openreading_compare",
            {
                "result_ids": [original["receipt"]["result_id"], resumed["receipt"]["result_id"]],
                "truth": {},
            },
        )
        assert (await retrieve(session, compared["result_id"]))["kind"] == "comparison_report"
        batch = await call(session, "openreading_batch", {"requests": [request(), request()]})
        batch = await finish(session, batch)
        assert batch["state"] == "succeeded"
        batch_content = await retrieve(session, batch["receipt"]["result_id"])
        assert len(batch_content["payload"]["items"]) == 2
        corpus = await call(
            session,
            "openreading_compare",
            {"result_ids": [batch["receipt"]["result_id"], batch["receipt"]["result_id"]]},
        )
        assert (await retrieve(session, corpus["result_id"]))["kind"] == "corpus_report"
        with slot(manager.root, 0):
            queued = await call(session, "openreading_parse", request())
            await call(session, "openreading_cancel_job", {"job_id": queued["job_id"]})
            cancelled = await finish(session, queued)
        assert cancelled["state"] == "cancelled" and cancelled["receipt"] is None
        assert exercised == catalog
