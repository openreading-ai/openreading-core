"""Complete delivery preserves content and measures the real escaped MCP response."""

import hashlib
import json
from pathlib import Path

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from openreading.artifacts.models import json_bytes
from openreading.mcp_server.tools import create_server
from tests.test_artifact_document import retain, rich_response


def rpc_bytes(result, request_id=1):
    message = types.JSONRPCMessage(
        types.JSONRPCResponse(
            jsonrpc="2.0",
            id=request_id,
            result=result.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
    )
    return (message.model_dump_json(by_alias=True, exclude_none=True) + "\n").encode()


@pytest.mark.asyncio
async def test_complete_auto_preserves_schema_ocr_warnings_and_exact_read(tmp_path):
    expected = rich_response('START "\\ 界😀 MIDDLE OCR END')
    service, identifier, passages = retain(
        tmp_path, expected, {"1": "none", "2": "ocr", "3": "native"}
    )
    try:
        async with create_connected_server_and_client_session(create_server(service)) as session:
            result = await session.call_tool(
                "openreading_get_document",
                {
                    "artifact_id": identifier,
                    "delivery": "auto",
                },
            )
            assert not result.isError
            payload = json.loads(result.content[0].text)
            assert payload["delivery"] == "tool_result"
            content = payload["content"]
            assert content["response"] == {k: v for k, v in expected.items() if k != "backend_raw"}
            assert content["page_origins"] == {"1": "none", "2": "ocr", "3": "native"}
            assert content["response"]["warnings"] == expected["warnings"]
            assert content["response"]["typed_fields"]["backend_raw"]["value"]
            assert "RAW-MUST-NOT-LEAVE-STORE" not in result.content[0].text
            assert "private/source.pdf" not in result.content[0].text
            assert len(json_bytes(content)) == payload["content_bytes"]
            assert hashlib.sha256(json_bytes(content)).hexdigest() == payload["content_sha256"]
            assert payload["parser_warnings"]["total"] == 1
            assert payload["parser_warnings"]["codes"] == [
                {"code": "partial_conversion", "count": 1}
            ]
            ref = content["evidence"][0]
            assert ref["text_origin"] == "ocr"
            read = await session.call_tool(
                "openreading_read",
                {
                    "artifact_id": identifier,
                    "evidence_ids": [ref["evidence_id"]],
                },
            )
            assert json.loads(read.content[0].text)["passages"][0]["text"] == passages[0].text
            assert result.structuredContent is None
    finally:
        service.close()


@pytest.mark.asyncio
async def test_oversize_is_saved_whole_and_explicit_file_uses_same_bytes(tmp_path):
    expected = rich_response('"\\界😀' * 400)
    service, identifier, _ = retain(tmp_path, expected, {"1": "none", "2": "ocr", "3": "native"})
    export_root = tmp_path.resolve() / "Exports"
    try:
        server = create_server(
            service, document_response_bytes=4096, document_export_root=export_root
        )
        async with create_connected_server_and_client_session(server) as session:
            results = []
            for delivery in ("auto", "file"):
                result = await session.call_tool(
                    "openreading_get_document",
                    {
                        "artifact_id": identifier,
                        "delivery": delivery,
                    },
                )
                assert not result.isError
                assert len(rpc_bytes(result)) <= 4096
                payload = json.loads(result.content[0].text)
                assert payload["delivery"] == "local_file"
                assert "content" not in payload
                path = Path(payload["local_path"])
                assert path.is_relative_to(export_root)
                data = path.read_bytes()
                assert len(data) == payload["content_bytes"]
                assert hashlib.sha256(data).hexdigest() == payload["content_sha256"]
                assert json.loads(data)["response"] == {
                    k: v for k, v in expected.items() if k != "backend_raw"
                }
                assert path.stat().st_mode & 0o777 == 0o600
                assert "upload" in payload["next_action"]
                results.append(payload)
            assert results[0]["local_path"] == results[1]["local_path"]
    finally:
        service.close()


@pytest.mark.parametrize("text", ["a" * 3000, '"\\' * 1500, "界😀" * 1500])
def test_budget_counts_escaping_unicode_and_actual_request_id(tmp_path, text):
    from openreading.mcp_server.delivery import deliver_document

    service, identifier, _ = retain(tmp_path, rich_response(text))
    try:
        request_id = 'req-"\\界😀-' * 20
        common = dict(mode="auto", root=None, request_id=request_id)
        candidate = deliver_document(service, identifier, budget=1_000_000, **common)
        size = len(rpc_bytes(candidate, request_id))
        assert size > len(candidate.content[0].text.encode())
        at_boundary = deliver_document(service, identifier, budget=size, **common)
        assert json.loads(at_boundary.content[0].text)["delivery"] == "tool_result"
        below = deliver_document(service, identifier, budget=size - 1, **common)
        assert json.loads(below.content[0].text)["delivery"] == "local_file"
        assert len(rpc_bytes(below, request_id)) <= size - 1
        # A shorter ID can fit when the long one cannot. File bytes cannot set this budget.
        shorter = deliver_document(
            service, identifier, mode="auto", root=None, request_id=1, budget=size - 1
        )
        assert json.loads(shorter.content[0].text)["delivery"] == "tool_result"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_complete_modes_refuse_cursors_and_model_export_paths(tmp_path):
    from mcp.shared.exceptions import McpError

    service, identifier, _ = retain(tmp_path, rich_response())
    try:
        async with create_connected_server_and_client_session(create_server(service)) as session:
            for mode in ("auto", "file"):
                result = await session.call_tool(
                    "openreading_get_document",
                    {
                        "artifact_id": identifier,
                        "delivery": mode,
                        "cursor": "private-secret",
                    },
                )
                assert result.isError
                assert json.loads(result.content[0].text)["error"]["code"] == "invalid_cursor"
                assert "private-secret" not in result.content[0].text
            with pytest.raises(McpError):
                await session.call_tool(
                    "openreading_get_document",
                    {
                        "artifact_id": identifier,
                        "delivery": "file",
                        "path": "/private-secret",
                    },
                )
    finally:
        service.close()


def test_export_never_overwrites_corrupt_entries_or_follows_links(tmp_path):
    from openreading.artifacts.delivery import save_export
    from openreading.artifacts.limits import ArtifactError

    root = tmp_path.resolve() / "exports"
    payload = b'{"complete":true}'
    path = save_export(root, "grant", payload)
    assert save_export(root, "grant", payload) == path
    path.write_bytes(b"unrelated")
    with pytest.raises(ArtifactError, match="artifact_corrupt"):
        save_export(root, "grant", payload)
    assert path.read_bytes() == b"unrelated"
    path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(payload)
    path.symlink_to(outside)
    with pytest.raises(ArtifactError, match="artifact_corrupt"):
        save_export(root, "grant", payload)
    assert outside.read_bytes() == payload
    assert not list(path.parent.glob("*.tmp"))
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ArtifactError):
        save_export(alias, "other", payload)
    assert not (root / "other").exists()


def test_export_write_failure_publishes_nothing_and_reports_storage(tmp_path, monkeypatch):
    import os

    from openreading.artifacts.delivery import save_export
    from openreading.artifacts.limits import ArtifactError

    def no_space(fd):
        raise OSError(28, "private-secret")

    monkeypatch.setattr(os, "fsync", no_space)
    root = tmp_path.resolve() / "exports"
    with pytest.raises(ArtifactError, match="storage_limit") as error:
        save_export(root, "grant", b"data")
    assert "private-secret" not in str(error.value)
    assert list((root / "grant").iterdir()) == []


def test_export_is_atomic_under_concurrent_publication_and_grant_isolated(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from openreading.artifacts.delivery import save_export

    root = tmp_path.resolve() / "exports"
    data = b"data" * 100_000
    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(lambda _: save_export(root, "one", data), range(8)))
    assert len(set(paths)) == 1
    assert paths[0].read_bytes() == data
    assert list(paths[0].parent.iterdir()) == [paths[0]]
    other = save_export(root, "two", data)
    assert other != paths[0]


def test_warning_summary_reports_omissions_without_altering_content():
    from openreading.artifacts.delivery import warning_summary

    warnings = [{"code": f"code{i:02}", "message": "provider detail"} for i in range(20)]
    warnings += [{"code": "x" * 200, "message": "long"}]
    summary = warning_summary({"warnings": warnings})
    assert summary.total == 21
    assert len(summary.codes) == 16
    assert summary.omitted == 5
    assert len(warnings) == 21


def test_delivery_options_reach_server_and_bad_budget_exits_before_store(
    tmp_path, monkeypatch, capsys
):
    from openreading.mcp_server import main as entry

    seen = []

    async def serve(config, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(entry, "serve", serve)
    root = tmp_path.resolve() / "input"
    root.mkdir()
    args = [
        "--profile",
        "local-document-proof-v1",
        "--input-root",
        str(root),
        "--artifact-root",
        str(tmp_path.resolve() / "store"),
    ]
    assert (
        entry.main(
            args
            + [
                "--document-response-bytes",
                "900000",
                "--document-export-root",
                str(tmp_path.resolve() / "exports"),
            ]
        )
        == 0
    )
    assert seen[0]["document_response_bytes"] == 900000
    assert seen[0]["document_export_root"] == tmp_path.resolve() / "exports"
    for budget in ("0", "4095", "-1"):
        assert entry.main(args + ["--document-response-bytes", budget]) == 2
    assert not (tmp_path / "store").exists()
    assert "Document response bytes" in capsys.readouterr().err


def test_real_stdio_matches_budget_with_escaped_request_ids(tmp_path):
    import os
    import select
    import subprocess
    import sys
    import time

    from openreading.mcp_server.delivery import deliver_document

    service, identifier, _ = retain(tmp_path, rich_response('"\\界😀' * 2000))
    request_id = 'read-"\\界😀-' * 25
    candidate = deliver_document(
        service, identifier, mode="auto", budget=1_000_000, root=None, request_id=request_id
    )
    boundary = len(rpc_bytes(candidate, request_id))
    root, store = service.config.input_root, service.config.artifact_root
    service.close()
    for budget, expected_delivery in ((boundary, "tool_result"), (boundary - 1, "local_file")):
        with subprocess.Popen(
            [
                sys.executable,
                "-m",
                "openreading.mcp_server.main",
                "--profile",
                "local-document-proof-v1",
                "--input-root",
                str(root),
                "--artifact-root",
                str(store),
                "--document-response-bytes",
                str(budget),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            pending = bytearray()

            def exchange(message, pending=pending):
                process.stdin.write(json_bytes(message) + b"\n")
                process.stdin.flush()
                deadline = time.monotonic() + 20
                while b"\n" not in pending:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and select.select([process.stdout], [], [], remaining)[0]
                    chunk = os.read(process.stdout.fileno(), 65536)
                    assert chunk
                    pending.extend(chunk)
                line, _, rest = pending.partition(b"\n")
                pending[:] = rest
                return bytes(line) + b"\n"

            try:
                initialized = exchange(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "delivery-test", "version": "1"},
                        },
                    }
                )
                assert "result" in json.loads(initialized)
                process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                process.stdin.flush()
                line = exchange(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {
                            "name": "openreading_get_document",
                            "arguments": {"artifact_id": identifier, "delivery": "auto"},
                        },
                    }
                )
                decoded = json.loads(line)
                assert decoded["id"] == request_id
                payload = json.loads(decoded["result"]["content"][0]["text"])
                assert payload["delivery"] == expected_delivery
                assert len(line) <= budget
                if expected_delivery == "tool_result":
                    assert len(line) == boundary
                    assert line == rpc_bytes(candidate, request_id)
            finally:
                process.stdin.close()
                process.wait(timeout=10)
            assert process.returncode == 0


@pytest.mark.asyncio
async def test_trusted_export_root_resolves_alias_once_before_requests(tmp_path):
    service, identifier, _ = retain(tmp_path, rich_response())
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "Downloads"
    alias.symlink_to(first, target_is_directory=True)
    try:
        server = create_server(service, document_export_root=alias / "OpenReading")
        alias.unlink()
        alias.symlink_to(second, target_is_directory=True)
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool(
                "openreading_get_document", {"artifact_id": identifier, "delivery": "file"}
            )
            assert not result.isError
            payload = json.loads(result.content[0].text)
            path = Path(payload["local_path"])
            assert path.is_relative_to(first.resolve())
            assert path.is_file()
            assert list(second.iterdir()) == []
    finally:
        service.close()


def test_oversize_skips_complete_result_materialization(tmp_path, monkeypatch):
    import openreading.mcp_server.delivery as delivery

    service, identifier, _ = retain(tmp_path, rich_response("x" * 20_000))

    def forbidden(**kwargs):
        pytest.fail("Oversized content must not build an inline result")

    monkeypatch.setattr(delivery, "CompleteResult", forbidden)
    try:
        result = delivery.deliver_document(
            service, identifier, mode="auto", budget=4096, root=None, request_id=1
        )
        payload = json.loads(result.content[0].text)
        assert payload["delivery"] == "local_file"
        assert Path(payload["local_path"]).stat().st_size == payload["content_bytes"]
    finally:
        service.close()


def test_export_permission_failure_is_reported_without_publishing(tmp_path, monkeypatch):
    import os

    from openreading.artifacts.delivery import save_export
    from openreading.artifacts.limits import ArtifactError

    def denied(*args, **kwargs):
        raise PermissionError(13, "private-path")

    monkeypatch.setattr(os, "link", denied)
    root = tmp_path.resolve() / "exports"
    with pytest.raises(ArtifactError, match="os_permission_denied") as error:
        save_export(root, "grant", b"data")
    assert "private-path" not in str(error.value)
    assert list((root / "grant").iterdir()) == []
