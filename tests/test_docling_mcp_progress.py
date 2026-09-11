"""MCP progress remains advisory and carries no extracted text or duplicate result."""

import json
import time
from types import SimpleNamespace

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from openreading.artifacts.limits import DoclingLimits
from openreading.artifacts.models import ImportReceipt
from openreading.mcp_server.tools import create_server


@pytest.mark.asyncio
async def test_progress_is_rate_limited_and_v2_description_uses_explicit_limits():
    class Service:
        config = SimpleNamespace(
            docling=SimpleNamespace(ocr=True),
            limits=DoclingLimits(
                pages=30, deadline_seconds=90, worker_memory_bytes=2**31, worker_idle_seconds=60
            ),
        )

        def import_document(self, path, *, cancelled, progress):
            progress("preflight")
            progress("conversion")
            time.sleep(1.05)
            progress("writing")
            time.sleep(0.05)
            return ImportReceipt(
                artifact_id="or1_" + "a" * 64,
                display_name="test.pdf",
                document_sha256="b" * 64,
                page_count=1,
                passage_count=1,
                reused=False,
                warnings=[],
            )

    seen = []

    async def progress(value, total, message):
        seen.append((value, total, message))

    async with create_connected_server_and_client_session(create_server(Service())) as session:
        description = (await session.list_tools()).tools[0].description
        assert "30 physical pages" in description
        assert "OCR is on" in description
        result = await session.call_tool(
            "openreading_import", {"path": "test.pdf"}, progress_callback=progress
        )
        assert not result.isError
        assert len(result.content) == 1
        assert json.loads(result.content[0].text)["schema_version"] == "0.3"
    assert seen == [(1, None, "preflight"), (3, None, "writing")]


@pytest.mark.asyncio
async def test_server_closes_service_after_transport_failure(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from openreading.artifacts import service
    from openreading.artifacts.limits import ProfileConfig
    from openreading.mcp_server import main, tools, transport

    closed = []
    fake = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(service, "ArtifactService", lambda config: fake)

    async def run(*args):
        raise RuntimeError("transport failed")

    server = SimpleNamespace(run=run, create_initialization_options=lambda: None)
    monkeypatch.setattr(tools, "create_server", lambda selected: server)

    @asynccontextmanager
    async def connection():
        yield None, None

    monkeypatch.setattr(transport, "cancellable_stdio", connection)
    with pytest.raises(BaseExceptionGroup):
        await main.serve(ProfileConfig(tmp_path, tmp_path / "store"))
    assert closed == [True]
