"""Result retrieval preserves complete JSON through measured MCP replies and local exports."""

import hashlib
import json
from pathlib import Path

import pytest
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session

from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import json_bytes
from openreading.artifacts.results import RetainedResults
from openreading.artifacts.service import ArtifactService
from openreading.mcp_server.results import deliver_result
from openreading.mcp_server.tools import create_server
from tests.test_mcp_delivery import rpc_bytes
from tests.test_retained_results import provenance, response


@pytest.fixture
def result_service(tmp_path):
    root = tmp_path.resolve()
    (root / "input").mkdir()
    service = ArtifactService(ProfileConfig(root / "input", root / "store"))
    try:
        yield service, RetainedResults(service.store)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_mcp_result_read_preserves_warnings_and_validates_arguments(result_service):
    service, store = result_service
    payload = response("partial")
    receipt = store.publish("normalized_response", payload, provenance())
    async with create_connected_server_and_client_session(create_server(service)) as session:
        catalog = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert not catalog["openreading_get_result"].annotations.readOnlyHint
        result = await session.call_tool("openreading_get_result", {"result_id": receipt.result_id})
        assert not result.isError
        reply = json.loads(result.content[0].text)
        assert reply["delivery"] == "tool_result"
        assert reply["content"]["payload"] == payload
        assert reply["content"]["provenance"]["adapters"] == {"synthetic": None}
        assert set(reply) == set(receipt.wire()) | {"delivery", "content"}
        missing = await session.call_tool(
            "openreading_get_result", {"result_id": "orr1_" + "f" * 64}
        )
        assert missing.isError
        assert json.loads(missing.content[0].text)["error"]["code"] == "result_not_found"
        for args in [
            {"result_id": "private-secret"},
            {"result_id": receipt.result_id, "path": "private-secret"},
        ]:
            with pytest.raises(McpError) as error:
                await session.call_tool("openreading_get_result", args)
            assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("text", ['quote"\\' * 700, "café界😀" * 700])
def test_inline_boundary_and_local_export_match_exact_bytes(result_service, text):
    service, store = result_service
    receipt = store.publish("normalized_response", response(text=text), provenance())
    request_id = 'id-"\\界' * 30
    common = dict(mode="auto", root=None, request_id=request_id)
    candidate = deliver_result(store, receipt.result_id, budget=1_000_000, **common)
    boundary = len(rpc_bytes(candidate, request_id))
    inline = deliver_result(store, receipt.result_id, budget=boundary, **common)
    assert json.loads(inline.content[0].text)["delivery"] == "tool_result"
    exported = deliver_result(store, receipt.result_id, budget=boundary - 1, **common)
    assert len(rpc_bytes(exported, request_id)) <= boundary - 1
    reply = json.loads(exported.content[0].text)
    assert reply["delivery"] == "local_file"
    raw = Path(reply["local_path"]).read_bytes()
    assert raw == json_bytes(store.load(receipt.result_id).wire())
    assert len(raw) == receipt.content_bytes
    assert hashlib.sha256(raw).hexdigest() == receipt.content_sha256
    forced = deliver_result(
        store, receipt.result_id, mode="file", budget=4096, root=None, request_id=1
    )
    assert json.loads(forced.content[0].text)["local_path"] == reply["local_path"]


def reconstruct(pages):
    """Independent consumer of pointer assignments and Unicode text spans."""
    output = None
    for page in pages:
        for part in page["fragments"]:
            tokens = [t.replace("~1", "/").replace("~0", "~") for t in part["path"].split("/")[1:]]
            if not tokens:
                output = part["value"]
                continue
            parent = output
            for token in tokens[:-1]:
                parent = parent[int(token)] if isinstance(parent, list) else parent[token]
            key = int(tokens[-1]) if isinstance(parent, list) else tokens[-1]
            if isinstance(parent, list) and key == len(parent):
                parent.append(None)
            if "value" in part:
                parent[key] = part["value"]
            else:
                prior = parent[key] if isinstance(parent, list) else parent.get(key)
                assert len(prior or "") == part["start"]
                assert part["end"] - part["start"] == len(part["text"])
                parent[key] = (prior or "") + part["text"]
    return output


def test_fragments_reconstruct_after_restart_and_bind_cursor(result_service):
    service, store = result_service
    payload = response(text='"\\界😀' * 2200)
    payload["typed_fields"] = {"a~/b": {"value": [None, {}, [], "é" * 1500]}}
    receipt = store.publish("normalized_response", payload, provenance())
    options = dict(mode="fragments", budget=4096, root=None, request_id='req-"\\界' * 5)
    cursor, pages = None, []
    while True:
        result = deliver_result(
            RetainedResults(service.store), receipt.result_id, cursor=cursor, **options
        )
        assert len(rpc_bytes(result, options["request_id"])) <= 4096
        page = json.loads(result.content[0].text)
        assert page["fragment_start"] == sum(len(p["fragments"]) for p in pages)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(pages) > 1
    assert sum(len(p["fragments"]) for p in pages) == pages[-1]["fragment_count"]
    assert reconstruct(pages) == store.load(receipt.result_id).wire()
    token = pages[0]["next_cursor"]
    other = store.publish("normalized_response", response(), provenance())
    for identifier, override in [
        (other.result_id, {}),
        (receipt.result_id, {"budget": 5000}),
        (receipt.result_id, {"mode": "auto"}),
    ]:
        with pytest.raises(ArtifactError, match="invalid_cursor"):
            deliver_result(store, identifier, cursor=token, **{**options, **override})


def test_undeliverable_receipt_creates_no_export(result_service):
    service, store = result_service
    receipt = store.publish("normalized_response", response(), provenance())
    with pytest.raises(ArtifactError, match="response_too_large"):
        deliver_result(
            store, receipt.result_id, mode="file", budget=4096, root=None, request_id="x" * 5000
        )
    assert not (service.config.artifact_root / "exports").exists()


@pytest.mark.asyncio
async def test_real_stdio_reads_retained_result_without_import(result_service):
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    service, store = result_service
    receipt = store.publish("normalized_response", response(text="Real stdio café"), provenance())
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "openreading.mcp_server.main",
            "--profile",
            "local-document-proof-v1",
            "--input-root",
            str(service.config.input_root),
            "--artifact-root",
            str(service.config.artifact_root),
            "--document-response-bytes",
            "4096",
        ],
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        reply = await session.call_tool("openreading_get_result", {"result_id": receipt.result_id})
        assert not reply.isError
        assert reply.structuredContent is None
        assert len(reply.content) == 1
        payload = json.loads(reply.content[0].text)
        assert payload["content"]["payload"]["document"]["text"] == "Real stdio café"
        assert payload["content_sha256"] == receipt.content_sha256


def test_invalid_or_indivisible_fragment_delivery_refuses(result_service):
    _, store = result_service
    payload = response()
    payload["typed_fields"] = {"x" * 8000: {"value": None}}
    receipt = store.publish("normalized_response", payload, provenance())
    with pytest.raises(ArtifactError, match="response_too_large"):
        deliver_result(
            store, receipt.result_id, mode="fragments", budget=4096, root=None, request_id=1
        )
    with pytest.raises(ValueError, match="Unknown"):
        deliver_result(
            store, receipt.result_id, mode="invented", budget=4096, root=None, request_id=1
        )
    small = store.publish("normalized_response", response(), provenance())
    with pytest.raises(ArtifactError, match="response_too_large"):
        deliver_result(
            store, small.result_id, mode="fragments", budget=4096, root=None, request_id="x" * 5000
        )
