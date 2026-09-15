"""Nonblocking stdio preserves framing and descriptor ownership under backpressure."""

import json
import os
from contextlib import ExitStack
from types import SimpleNamespace

import anyio
import pytest
from mcp import types
from mcp.shared.message import SessionMessage

from openreading.mcp_server import transport


@pytest.mark.asyncio
async def test_transport_redacts_protocol_errors_and_restores_descriptors(monkeypatch):
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    with ExitStack() as stack:
        stdin = stack.enter_context(os.fdopen(input_read, "r"))
        source = stack.enter_context(os.fdopen(input_write, "wb", buffering=0))
        stdout = stack.enter_context(os.fdopen(output_write, "w"))
        sink = stack.enter_context(os.fdopen(output_read, "rb", buffering=0))
        monkeypatch.setattr(transport, "sys", SimpleNamespace(stdin=stdin, stdout=stdout))
        os.set_blocking(output_read, False)
        assert os.get_blocking(input_read) and os.get_blocking(output_write)
        with anyio.fail_after(5):
            async with transport.cancellable_stdio() as (reader, writer):
                source.write(b'{"secret":"private"}\n')
                error = await reader.receive()
                assert isinstance(error, ValueError)
                assert str(error) == "Invalid MCP protocol message."
                source.write(
                    b'{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"openreading_import","arguments":"private"}}\n'
                )
                await anyio.wait_readable(output_read)
                failure = json.loads(sink.readline())
                assert failure["id"] == 8
                assert failure["error"] == {"code": -32602, "message": "Invalid request parameters"}
                source.write(
                    b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":{"secret":"private"}}}\n'
                )
                assert str(await reader.receive()) == "Invalid MCP protocol message."
                request = types.JSONRPCMessage(
                    types.JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized")
                )
                source.write(request.model_dump_json().encode() + b"\n")
                received = await reader.receive()
                assert isinstance(received, SessionMessage)
                assert received.message == request
                await writer.send(received)
                await anyio.wait_readable(output_read)
                assert json.loads(sink.readline())["method"] == "notifications/initialized"
                await writer.aclose()
                source.close()
                with pytest.raises(anyio.EndOfStream):
                    await reader.receive()
            assert os.get_blocking(input_read) and os.get_blocking(output_write)


@pytest.mark.asyncio
async def test_input_preserves_split_utf8_and_unterminated_final_line(monkeypatch):
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "r") as stdin, os.fdopen(write_fd, "wb", buffering=0) as source:
        monkeypatch.setattr(transport, "sys", SimpleNamespace(stdin=stdin))
        os.set_blocking(read_fd, False)
        reader = transport._Input()
        first, rest = "界\nlast".encode()[:1], "界\nlast".encode()[1:]
        source.write(first)

        async def finish():
            await anyio.sleep(0.01)
            source.write(rest)
            source.close()

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(finish)
                assert await reader.readline() == "界\n"
                assert await reader.readline() == "last"
                assert await reader.readline() == ""


@pytest.mark.asyncio
async def test_output_backpressure_preserves_complete_utf8(monkeypatch):
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0), os.fdopen(write_fd, "w") as stdout:
        monkeypatch.setattr(transport, "sys", SimpleNamespace(stdout=stdout))
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        text = "界🙂" * 40000
        received = bytearray()

        async def drain():
            await anyio.sleep(0.01)
            while len(received) < len(text.encode()):
                try:
                    received.extend(os.read(read_fd, 8192))
                except BlockingIOError:
                    await anyio.wait_readable(read_fd)

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(drain)
                writer = transport._Output()
                assert await writer.write(text) == len(text)
                await writer.flush()
        assert received.decode() == text
