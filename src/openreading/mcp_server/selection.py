"""Coordinate a trusted local chooser without granting model-selected paths.

A launcher explicitly passes a SelectionProvider to create_server or serve. No discovery,
GUI dependency, environment variable, or executable lookup installs one in headless core.
The provider's async context manager yields a relative copied-file reference, or None
when the user cancels. It must dismiss its UI and roll back its own copy on any exception,
including cancellation during validation. Cleanup must finish before context exit.
A normal exit transfers retention ownership to the local intake implementation.

Selection permits one pending dialog per server, without blocking imports or retrieval.
The default 120-second deadline covers choosing, copying, and reference validation.
Trusted launchers may shorten it or raise it to 180 seconds. These are finite safety
limits, not claims about every host's deadline. Cancellation cleanup can exceed them.
The provider must cooperate with cancellation; core cannot forcibly terminate arbitrary
in-process Python code. Providers should isolate native dialogs in a reapable child.

Core validates the reference through the service's opened input grant, checking regular
file status and source size. Import still owns PDF validation, hashing, and extraction.
No returned receipt proves that a disconnected client received it. Host Stop without
protocol cancellation cannot stop selection; local Cancel and the deadline remain active.
"""

from __future__ import annotations

import math
import os
from contextlib import AbstractAsyncContextManager
from pathlib import PurePosixPath
from typing import Protocol

import anyio
from anyio.lowlevel import checkpoint

from openreading.artifacts.service import ArtifactService
from openreading.types.selection import SelectionFailure, SelectionReceipt


class SelectionProvider(Protocol):
    def select(self) -> AbstractAsyncContextManager[str | None]:
        """Yield only this call's private copied reference; roll back on exceptional exit."""
        ...


class SelectionCoordinator:
    def __init__(
        self,
        service: ArtifactService,
        provider: SelectionProvider | None,
        timeout_seconds: float,
    ):
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 180
        ):
            raise ValueError("Selection timeout must be positive and at most 180 seconds.")
        self.service = service
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.pending = False

    async def select(self) -> SelectionReceipt | SelectionFailure:
        await checkpoint()
        if self.provider is None:
            return SelectionFailure.from_code("selection_unavailable")
        if self.pending:
            return SelectionFailure.from_code("busy")
        # There is no await between admission and ownership, so another call cannot enter.
        self.pending = True
        try:
            with anyio.fail_after(self.timeout_seconds):
                async with self.provider.select() as reference:
                    if reference is None:
                        return SelectionFailure.from_code("selection_cancelled")
                    if not isinstance(reference, str):
                        raise ValueError("Invalid provider reference.")
                    with self.service.store.source(reference) as opened:
                        size = os.fstat(opened).st_size
                        if not 0 < size <= self.service.config.limits.source_bytes:
                            raise ValueError("Invalid selected size.")
                    receipt = SelectionReceipt(
                        path=reference,
                        display_name=PurePosixPath(reference).name,
                        source_bytes=size,
                    )
                    # A cancellation arriving during validation must reach provider rollback.
                    await checkpoint()
                    return receipt
        except TimeoutError:
            return SelectionFailure.from_code("selection_timeout")
        except Exception:
            return SelectionFailure.from_code("selection_failed")
        finally:
            self.pending = False
