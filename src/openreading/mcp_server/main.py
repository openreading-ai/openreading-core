"""Start an explicit local or general document profile with MCP-only stdout traffic.

Exit 0 means normal transport closure, 2 means invalid configuration or missing extras,
and 130 means interruption. Ambient environment variables never select roots or execution scope.
The launcher supplies all configuration through arguments before accepting tool calls.

General execution snapshots its own backend and strategy scopes before accepting requests.
Local planning flags cannot authorize execution, and local profiles refuse execution flags.
For example, --execute-backend pymupdf grants execution only in general-execution-v1.
General startup initializes no local extraction engine and provides no native file chooser.

Environment variables this module reads
--------------------------------------
Only names selected with --execution-env are read for forwarding to execution workers.
For example, --execution-env REDUCTO_API_KEY forwards that value without displaying it.
Absent or invalid names refuse startup. Other ambient credentials and configuration stay excluded.
ExecutionAttempt validates reserved worker paths before any execution job can be accepted.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openreading.mcp_server.routing import RoutingConfig
    from openreading.mcp_server.selection import SelectionProvider

from openreading.adapters.docling_local.config import LocalDoclingConfig
from openreading.artifacts.limits import ArtifactError, DoclingLimits, GeneralLimits, ProfileConfig


def arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--execution-config",
        type=Path,
        help="explicit execution configuration snapshot for general-execution-v1 only",
    )
    parser.add_argument(
        "--execute-backend",
        action="append",
        help="authorize a general execution backend; repeat for each backend (default: none)",
    )
    parser.add_argument(
        "--execute-strategy",
        action="append",
        help="authorize a configured general execution strategy; repeat for each name (default: none)",
    )
    parser.add_argument(
        "--execution-env",
        action="append",
        metavar="NAME",
        help="forward only this existing environment variable to execution workers; repeat per name",
    )
    parser.add_argument(
        "--execution-deadline-seconds",
        type=float,
        help="positive general execution deadline in seconds (default: no deadline)",
    )
    parser.add_argument(
        "--execution-concurrency",
        type=int,
        help="positive general execution concurrency (default: 1)",
    )
    parser.add_argument(
        "--routing-config",
        type=Path,
        help="explicit openreading.yaml snapshot for route planning; never discovered from the environment",
    )
    parser.add_argument(
        "--allow-backend",
        action="append",
        help="authorize a backend for route planning; repeat for each backend (default: local import backend)",
    )
    parser.add_argument(
        "--document-response-bytes",
        type=int,
        default=1_000_000,
        help="complete-document delivery budget in serialized MCP bytes (default: 1000000)",
    )
    parser.add_argument(
        "--document-export-root",
        type=Path,
        help="trusted export directory (default: exports inside the private artifact store)",
    )
    parser.add_argument(
        "--profile",
        required=True,
        choices=["local-document-proof-v1", "local-document-proof-v2", "general-execution-v1"],
        help="local evidence or independently authorized general execution profile",
    )
    parser.add_argument(
        "--profile-config",
        type=Path,
        help="closed Docling setup JSON with assets and optional operator limits",
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
    profile = getattr(args, "profile", "local-document-proof-v1")
    if profile in {"local-document-proof-v1", "general-execution-v1"}:
        if path is not None:
            raise ArtifactError("configuration_required")
        if profile == "general-execution-v1":
            return ProfileConfig(args.input_root, args.artifact_root, GeneralLimits())
        return ProfileConfig(args.input_root, args.artifact_root)
    if profile != "local-document-proof-v2":
        raise ArtifactError("configuration_required")
    try:
        if path is None:
            raise ValueError
        with path.open("rb") as stream:
            data = stream.read(16385)
        if len(data) > 16384:
            raise ValueError
        value = json.loads(data)
        if not {
            "pages",
            "deadline_seconds",
            "worker_memory_bytes",
            "worker_idle_seconds",
            "docling",
        } <= set(value) or set(value) - {
            "pages",
            "deadline_seconds",
            "worker_memory_bytes",
            "worker_idle_seconds",
            "docling",
            "source_bytes",
            "extraction_bytes",
            "store_bytes",
        }:
            raise ValueError
        docling = LocalDoclingConfig.from_wire(value.pop("docling"))
        return ProfileConfig(args.input_root, args.artifact_root, DoclingLimits(**value), docling)
    except (OSError, ValueError, TypeError):
        raise ArtifactError("configuration_required") from None


async def serve(
    config: ProfileConfig,
    *,
    routing_config: RoutingConfig | None = None,
    selection_provider: SelectionProvider | None = None,
    selection_timeout_seconds: float | None = 120,
    document_response_bytes: int = 1_000_000,
    document_export_root: Path | None = None,
) -> None:
    import anyio

    from openreading.artifacts.service import ArtifactService
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
        service = ArtifactService(config)
        try:
            server = create_server(
                service,
                routing_config=routing_config,
                selection_provider=selection_provider,
                selection_timeout_seconds=selection_timeout_seconds,
                document_response_bytes=document_response_bytes,
                document_export_root=document_export_root,
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
    selection_timeout_seconds: float | None = 120,
) -> int:
    parser = argparse.ArgumentParser(
        description="Serve local document evidence or scoped general execution over stdio MCP."
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
    selection_timeout_seconds: float | None = 120,
) -> int:
    if os.name != "posix":
        print(
            "MCP profiles require a POSIX platform. Windows is not supported.",
            file=sys.stderr,
        )
        return 2
    try:
        import anyio

        from openreading.cli.app import _terminate_as_interrupt
        from openreading.mcp_server.delivery import validate_delivery_config
        from openreading.mcp_server.execution import ExecutionConfig, ExecutionRefused
        from openreading.mcp_server.execution_process import ExecutionError
        from openreading.mcp_server.selection import validate_selection_timeout

        general = getattr(args, "profile", None) == "general-execution-v1"
        execution_fields = (
            "execution_config",
            "execute_backend",
            "execute_strategy",
            "execution_env",
            "execution_deadline_seconds",
            "execution_concurrency",
        )
        if not general and any(getattr(args, name, None) is not None for name in execution_fields):
            raise ExecutionRefused("invalid_configuration")
        if general and (
            getattr(args, "routing_config", None) is not None
            or getattr(args, "allow_backend", None) is not None
        ):
            raise ExecutionRefused("invalid_configuration")

        budget = getattr(args, "document_response_bytes", 1_000_000)
        export_root = getattr(args, "document_export_root", None)
        try:
            validate_delivery_config(budget, export_root)
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
            if general:
                from functools import partial

                from openreading.mcp_server.general import serve as general_serve

                authority = ExecutionConfig.from_operator(
                    config=getattr(args, "execution_config", None),
                    allowed_backends=getattr(args, "execute_backend", None) or (),
                    allowed_strategies=getattr(args, "execute_strategy", None) or (),
                )
                environment = {}
                for name in getattr(args, "execution_env", None) or ():
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name not in os.environ:
                        raise ExecutionRefused("invalid_configuration")
                    environment[name] = os.environ[name]
                deadline = getattr(args, "execution_deadline_seconds", None)
                concurrency = getattr(args, "execution_concurrency", None)
                concurrency = 1 if concurrency is None else concurrency
                if (
                    (
                        deadline is not None
                        and (
                            type(deadline) not in (int, float)
                            or not math.isfinite(deadline)
                            or deadline <= 0
                        )
                    )
                    or type(concurrency) is not int
                    or concurrency <= 0
                ):
                    raise ExecutionRefused("invalid_configuration")
                with _terminate_as_interrupt():
                    anyio.run(
                        partial(
                            general_serve,
                            authority=authority,
                            environment=environment,
                            deadline_seconds=deadline,
                            concurrency=concurrency,
                            document_response_bytes=budget,
                            document_export_root=export_root,
                        ),
                        config,
                    )
                return 0
            routing = None
            route_path = getattr(args, "routing_config", None)
            allowed_backends = getattr(args, "allow_backend", None)
            if route_path is not None or allowed_backends is not None:
                from openreading.mcp_server.routing import RoutingConfig

                routing = RoutingConfig.from_operator(
                    "docling_local" if config.docling is not None else "pymupdf",
                    config=route_path,
                    allowed_backends=allowed_backends,
                )
        except (OSError, RuntimeError, ValueError):
            raise ArtifactError("configuration_required") from None
        with _terminate_as_interrupt():
            from functools import partial

            operation = (
                serve
                if selection_provider is None
                and selection_timeout_seconds == 120
                and budget == 1_000_000
                and export_root is None
                and routing is None
                else partial(
                    serve,
                    routing_config=routing,
                    selection_provider=selection_provider,
                    selection_timeout_seconds=selection_timeout_seconds,
                    document_response_bytes=budget,
                    document_export_root=export_root,
                )
            )
            anyio.run(operation, config)
        return 0
    except ImportError:
        print(
            "Install openreading[agent] for general execution, openreading[agent,pymupdf] for v1, "
            "or openreading[agent,docling-local] for v2.",
            file=sys.stderr,
        )
        return 2
    except (ExecutionRefused, ExecutionError):
        print("Invalid MCP execution configuration.", file=sys.stderr)
        return 2
    except ArtifactError as error:
        print(error.envelope().error.message, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
