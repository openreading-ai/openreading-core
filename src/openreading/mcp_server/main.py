"""Start the explicit local document profile; stdout contains MCP traffic only.

Exit 0 means normal transport closure, 2 means invalid configuration or missing extras,
and 130 means interruption. No environment variable selects roots or backend behavior.
The launcher supplies all configuration through arguments before accepting tool calls.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from contextlib import nullcontext
from pathlib import Path

from openreading.artifacts.limits import ArtifactError, ProfileConfig


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile",
        required=True,
        choices=["local-document-proof-v1"],
        help="fixed local extraction and retrieval limits",
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


async def serve(config: ProfileConfig) -> None:
    import anyio

    from openreading.artifacts.service import ArtifactService
    from openreading.mcp_server.tools import create_server
    from openreading.mcp_server.transport import cancellable_stdio

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
        server = create_server(ArtifactService(config))
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
    if interrupted:
        raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve bounded local document evidence over stdio MCP."
    )
    arguments(parser)
    args = parser.parse_args(argv)
    return launch(args)


def launch(args: argparse.Namespace) -> int:
    if os.name != "posix":
        print(
            "The local MCP profile requires a POSIX platform; Windows is not supported.",
            file=sys.stderr,
        )
        return 2
    try:
        import anyio

        from openreading.cli.app import _terminate_as_interrupt

        if not args.input_root.is_absolute() or not args.artifact_root.is_absolute():
            raise ArtifactError("configuration_required")
        try:
            config = ProfileConfig(args.input_root.resolve(), args.artifact_root.resolve())
        except (OSError, RuntimeError):
            raise ArtifactError("configuration_required") from None
        with _terminate_as_interrupt():
            anyio.run(serve, config)
        return 0
    except ImportError:
        print("Install openreading[agent,pymupdf] to run the local agent profile.", file=sys.stderr)
        return 2
    except ArtifactError as error:
        print(error.envelope().error.message, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
