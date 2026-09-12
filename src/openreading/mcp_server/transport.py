"""Keep stdio cancellation independent of a client closing its input pipe.

The SDK transport accepts async file objects, but its default thread-backed reads wait
for EOF before cancellation completes. Nonblocking descriptors let stop signals cancel
both reads and writes, including a client that stops consuming protocol output.
Malformed protocol exceptions are replaced before SDK logging can echo confidential input.
Descriptor blocking modes are restored when an embedded caller regains control.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

import anyio
from anyio.lowlevel import checkpoint
from mcp import types
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from pydantic import ValidationError


class _Input(anyio.AsyncFile[str]):
    def __init__(self):
        super().__init__(sys.stdin)
        self.fd = sys.stdin.fileno()
        self.pending = bytearray()

    async def readline(self) -> str:
        while b"\n" not in self.pending:
            await checkpoint()
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                await anyio.wait_readable(self.fd)
                continue
            if not chunk:
                line = bytes(self.pending)
                self.pending.clear()
                return line.decode("utf-8", errors="replace")
            self.pending.extend(chunk)
        end = self.pending.index(b"\n") + 1
        line = bytes(self.pending[:end])
        del self.pending[:end]
        return line.decode("utf-8", errors="replace")


class _Output(anyio.AsyncFile[str]):
    def __init__(self):
        super().__init__(sys.stdout)
        self.fd = sys.stdout.fileno()

    async def write(self, text: str) -> int:
        remaining = memoryview(text.encode("utf-8"))
        while remaining:
            await checkpoint()
            try:
                written = os.write(self.fd, remaining)
            except BlockingIOError:
                await anyio.wait_writable(self.fd)
                continue
            remaining = remaining[written:]
        return len(text)

    async def flush(self) -> None:
        # Writes already reach the descriptor without a Python buffering layer.
        await checkpoint()


@asynccontextmanager
async def cancellable_stdio():
    descriptors = [sys.stdin.fileno(), sys.stdout.fileno()]
    modes = [os.get_blocking(fd) for fd in descriptors]
    try:
        for fd in descriptors:
            os.set_blocking(fd, False)
        async with stdio_server(stdin=_Input(), stdout=_Output()) as (reader, writer):
            sender, sanitized = anyio.create_memory_object_stream[SessionMessage | Exception](0)

            async def forward():
                async with sender:
                    async for message in reader:
                        if isinstance(message, Exception):
                            message = ValueError("Invalid MCP protocol message.")
                        else:
                            root = message.message.root
                            contract = (
                                types.ClientRequest
                                if isinstance(root, types.JSONRPCRequest)
                                else types.ClientNotification
                                if isinstance(root, types.JSONRPCNotification)
                                else None
                            )
                            try:
                                if contract is not None:
                                    contract.model_validate(
                                        root.model_dump(
                                            by_alias=True, mode="json", exclude_none=True
                                        )
                                    )
                            except ValidationError:
                                # Session validation logs its offending input before responding.
                                if isinstance(root, types.JSONRPCRequest):
                                    await writer.send(
                                        SessionMessage(
                                            types.JSONRPCMessage(
                                                types.JSONRPCError(
                                                    jsonrpc="2.0",
                                                    id=root.id,
                                                    error=types.ErrorData(
                                                        code=-32602,
                                                        message="Invalid request parameters",
                                                    ),
                                                )
                                            )
                                        )
                                    )
                                else:
                                    await sender.send(ValueError("Invalid MCP protocol message."))
                                continue
                        await sender.send(message)

            async with anyio.create_task_group() as group, sanitized:
                group.start_soon(forward)
                try:
                    yield sanitized, writer
                finally:
                    group.cancel_scope.cancel()
    finally:
        for fd, blocking in zip(descriptors, modes, strict=True):
            os.set_blocking(fd, blocking)
