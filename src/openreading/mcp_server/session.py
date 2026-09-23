"""Serve document tools through stdio using a caller-supplied acquisition service.

The fixed service factory owns processing. No local parser is selected implicitly.
Signal and transport closure release the service while detached imports keep their state.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from openreading.artifacts.limits import ProfileConfig
from openreading.artifacts.retained import RetainedService as ArtifactService

if TYPE_CHECKING:
    from openreading.artifacts.jobs import ImportExecution
    from openreading.mcp_server.selection import SelectionProvider


async def serve(
    config: ProfileConfig,
    *,
    selection_provider: SelectionProvider | None = None,
    selection_timeout_seconds: float | None = 120,
    document_response_bytes: int = 1_000_000,
    document_export_root: Path | None = None,
    service_factory: Callable[[ProfileConfig], ArtifactService],
    execution: ImportExecution | None = None,
) -> None:
    import anyio

    from openreading.mcp_server.delivery import validate_delivery_config
    from openreading.mcp_server.selection import validate_selection_timeout
    from openreading.mcp_server.tools import create_server
    from openreading.mcp_server.transport import cancellable_stdio

    validate_selection_timeout(selection_timeout_seconds)
    validate_delivery_config(document_response_bytes, document_export_root)
    interrupted = False
    signals = []
    if threading.current_thread() is threading.main_thread():
        signals = [
            value
            for value in (signal.SIGINT, signal.SIGTERM)
            if signal.getsignal(value) not in (None, signal.SIG_IGN)
        ]
    receiver = anyio.open_signal_receiver(*signals) if signals else nullcontext()
    with receiver as received:
        service = service_factory(config)
        try:
            server = create_server(
                service,
                selection_provider=selection_provider,
                selection_timeout_seconds=selection_timeout_seconds,
                document_response_bytes=document_response_bytes,
                document_export_root=document_export_root,
                **({"execution": execution} if execution is not None else {}),
            )
            async with anyio.create_task_group() as group, cancellable_stdio() as (reader, writer):

                async def stop_on_signal():
                    nonlocal interrupted
                    assert received is not None
                    async for _ in received:
                        interrupted = True
                        group.cancel_scope.cancel()
                        break

                if received is not None:
                    group.start_soon(stop_on_signal)
                await server.run(reader, writer, server.create_initialization_options())
                group.cancel_scope.cancel()
        finally:
            service.close()
    if interrupted:
        raise KeyboardInterrupt
