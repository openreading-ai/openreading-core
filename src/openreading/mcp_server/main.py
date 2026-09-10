"""Start the explicit local document profile; stdout contains MCP traffic only.

Exit 0 means normal transport closure, 2 means invalid configuration or missing extras,
and 130 means interruption. No environment variable selects roots or backend behavior.
The launcher supplies all configuration through arguments before accepting tool calls.
"""

from __future__ import annotations

import argparse
import sys
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
    from mcp.server.stdio import stdio_server

    from openreading.artifacts.service import ArtifactService
    from openreading.mcp_server.tools import create_server

    server = create_server(ArtifactService(config))
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve bounded local document evidence over stdio MCP."
    )
    arguments(parser)
    args = parser.parse_args(argv)
    return launch(args)


def launch(args: argparse.Namespace) -> int:
    try:
        import anyio

        anyio.run(serve, ProfileConfig(args.input_root, args.artifact_root))
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
