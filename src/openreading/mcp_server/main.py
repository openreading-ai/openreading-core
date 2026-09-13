"""Start the explicit local document profile; stdout contains MCP traffic only.

Exit 0 means normal transport closure, 2 means invalid configuration or missing extras,
and 130 means interruption. No environment variable selects roots or backend behavior.
The launcher supplies all configuration through arguments before accepting tool calls.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openreading.mcp_server.selection import SelectionProvider

from openreading.adapters.docling_local.config import LocalDoclingConfig
from openreading.artifacts.limits import ArtifactError, DoclingLimits, ProfileConfig


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        required=True,
        choices=["local-document-proof-v1", "local-document-proof-v2"],
        help="fixed local extraction and retrieval limits",
    )
    parser.add_argument(
        "--profile-config",
        type=Path,
        help="closed Docling setup JSON with assets and measured resource limits",
    )
    parser.add_argument(
        "--input-root",
        required=True,
        type=Path,
        help="absolute directory granting access to document inputs",
    )
    parser.add_argument(
        "--artifact-root",
        required=True,
        type=Path,
        help="separate absolute directory retaining sources and evidence",
    )


def profile_config(args: argparse.Namespace) -> ProfileConfig:
    path = getattr(args, "profile_config", None)
    if getattr(args, "profile", "local-document-proof-v1") == "local-document-proof-v1":
        if path is not None:
            raise ArtifactError("configuration_required")
        return ProfileConfig(args.input_root, args.artifact_root)
    try:
        if path is None:
            raise ValueError
        with path.open("rb") as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError
        value = json.loads(data)
        if set(value) != {
            "pages",
            "deadline_seconds",
            "worker_memory_bytes",
            "worker_idle_seconds",
            "docling",
        }:
            raise ValueError
        docling = LocalDoclingConfig.from_wire(value.pop("docling"))
        return ProfileConfig(args.input_root, args.artifact_root, DoclingLimits(**value), docling)
    except (OSError, ValueError, TypeError):
        raise ArtifactError("configuration_required") from None


async def serve(
    config: ProfileConfig,
    *,
    selection_provider: SelectionProvider | None = None,
    selection_timeout_seconds: float = 120,
) -> None:
    import anyio

    from openreading.artifacts.service import ArtifactService
    from openreading.mcp_server.selection import validate_selection_timeout
    from openreading.mcp_server.tools import create_server
    from openreading.mcp_server.transport import cancellable_stdio

    validate_selection_timeout(selection_timeout_seconds)
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
        service = ArtifactService(config)
        try:
            server = create_server(
                service,
                selection_provider=selection_provider,
                selection_timeout_seconds=selection_timeout_seconds,
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


def main(
    argv: list[str] | None = None,
    *,
    selection_provider: SelectionProvider | None = None,
    selection_timeout_seconds: float = 120,
) -> int:
    parser = argparse.ArgumentParser(
        description="Serve bounded local document evidence over stdio MCP."
    )
    arguments(parser)
    args = parser.parse_args(argv)
    return launch(
        args,
        selection_provider=selection_provider,
        selection_timeout_seconds=selection_timeout_seconds,
    )


def launch(
    args: argparse.Namespace,
    *,
    selection_provider: SelectionProvider | None = None,
    selection_timeout_seconds: float = 120,
) -> int:
    if os.name != "posix":
        print(
            "The local MCP profile requires a POSIX platform; Windows is not supported.",
            file=sys.stderr,
        )
        return 2
    try:
        import anyio

        from openreading.cli.app import _terminate_as_interrupt
        from openreading.mcp_server.selection import validate_selection_timeout

        try:
            validate_selection_timeout(selection_timeout_seconds)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2

        if not args.input_root.is_absolute() or not args.artifact_root.is_absolute():
            raise ArtifactError("configuration_required")
        try:
            args.input_root = args.input_root.resolve()
            args.artifact_root = args.artifact_root.resolve()
            config = profile_config(args)
        except (OSError, RuntimeError):
            raise ArtifactError("configuration_required") from None
        with _terminate_as_interrupt():
            from functools import partial

            operation = (
                serve
                if selection_provider is None and selection_timeout_seconds == 120
                else partial(
                    serve,
                    selection_provider=selection_provider,
                    selection_timeout_seconds=selection_timeout_seconds,
                )
            )
            anyio.run(operation, config)
        return 0
    except ImportError:
        print(
            "Install openreading[agent,pymupdf] for v1 or openreading[agent,docling-local] for v2.",
            file=sys.stderr,
        )
        return 2
    except ArtifactError as error:
        print(error.envelope().error.message, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
