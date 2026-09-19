"""Coordinate a trusted local chooser without granting model-selected paths.

A batch provider yields SelectionBatch with copied references and aggregate skipped-entry counts.
Receipt continuations pass only a cursor and never reopen the chooser. Each copied PDF uses
the existing import tool and its own job and artifact. Legacy providers may still yield one string.

A launcher explicitly passes a SelectionProvider to create_server or serve. No discovery,
GUI dependency, environment variable, or executable lookup installs one in headless core.
The provider's async context manager yields a relative copied-file reference, or None
when the user cancels. It must dismiss its UI and roll back its own copy on any exception,
including cancellation during validation. Cleanup must finish before context exit.
Every asynchronous cleanup must use ``with anyio.CancelScope(shield=True)`` around
its awaits, including cleanup when cancellation interrupts acquisition before yield.
For example, shield child termination and ``await child.aclose()`` in a finally block.
Core cannot shield code inside a provider's interrupted ``__aenter__`` call.
The provider must propagate cancellation after cleanup, never suppress it.
A normal exit transfers retention ownership to the local intake implementation.

Selection permits one pending dialog per server. Cooperative providers leave retrieval
and imports responsive by moving blocking GUI, copy, and filesystem work off the event loop.
The default 120-second deadline covers choosing, copying, and reference validation.
Trusted launchers may pass None to disable that deadline, or set up to 180 seconds.
A host can still cancel the request independently. Cancellation cleanup can exceed a deadline.
The provider must cooperate with cancellation; core cannot forcibly terminate arbitrary
in-process Python code. Providers should isolate native dialogs in a reapable child.

Core validates the reference through the service's opened input grant, checking regular
file status and source size. Import still owns PDF validation, hashing, and extraction.
References pass through unchanged. Providers must generate opaque intake directories,
not mirror original folder names, and roll back only copies owned by this selection.
Core checks access and size, not provider ownership or source-path confidentiality.
No returned receipt proves that a disconnected client received it. Host Stop without
protocol cancellation cannot stop selection. Local Cancel and any configured deadline remain active.
"""

from __future__ import annotations

import asyncio
import math
import os
import threading
from contextlib import AbstractAsyncContextManager, suppress
from pathlib import PurePosixPath
from typing import Protocol

import anyio
from anyio.lowlevel import checkpoint
from anyio.to_thread import run_sync

from openreading.artifacts.service import ArtifactService
from openreading.mcp_server.selection_pages import SelectionPages
from openreading.types.selection import (
    SelectionBatch,
    SelectionFailure,
    SelectionPage,
    SelectionReceipt,
)


class SelectionProvider(Protocol):
    def select(self) -> AbstractAsyncContextManager[str | SelectionBatch | dict | None]:
        """Yield an owned copy; shield async rollback, including failed acquisition.

        Blocking work belongs off the event loop. Cancellation must propagate after
        cleanup completes, so core retains admission until the provider has finished.
        """
        ...


def validate_selection_timeout(timeout_seconds: float | None) -> None:
    """Reject invalid launcher deadlines before opening grants or computing identity."""
    if timeout_seconds is None:
        return
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 180
    ):
        raise ValueError("Selection timeout must be positive and at most 180 seconds.")


async def _publish(pages: SelectionPages, batch: SelectionBatch) -> SelectionPage:
    cancelled = threading.Event()

    def work():
        try:
            return pages.publish(batch, cancelled=cancelled.is_set)
        except Exception as error:
            return error

    future = asyncio.get_running_loop().run_in_executor(None, work)
    try:
        result = await asyncio.shield(future)
    except anyio.get_cancelled_exc_class():
        cancelled.set()
        with anyio.CancelScope(shield=True):
            await asyncio.shield(future)
        raise
    if isinstance(result, Exception):
        raise result
    return result


class SelectionCoordinator:
    def __init__(
        self,
        service: ArtifactService,
        provider: SelectionProvider | None,
        timeout_seconds: float | None,
    ):
        validate_selection_timeout(timeout_seconds)
        self.service = service
        self.provider = provider
        self.timeout_seconds = timeout_seconds
        self.pending = False

    async def select(
        self, cursor: str | None = None
    ) -> SelectionReceipt | SelectionPage | SelectionFailure:
        await checkpoint()
        if cursor is not None:
            try:
                return await run_sync(SelectionPages(self.service).read, cursor)
            except Exception:
                return SelectionFailure.from_code("selection_failed")
        if self.provider is None:
            return SelectionFailure.from_code("selection_unavailable")
        if self.pending:
            return SelectionFailure.from_code("busy")
        # There is no await between admission and ownership, so another call cannot enter.
        self.pending = True
        pages = None
        accepted = False
        try:
            with anyio.fail_after(self.timeout_seconds):
                async with self.provider.select() as reference:
                    if reference is None:
                        return SelectionFailure.from_code("selection_cancelled")
                    if isinstance(reference, dict):
                        reference = SelectionBatch(**reference)
                    if isinstance(reference, SelectionBatch):
                        pages = SelectionPages(self.service)
                        receipt_page = await _publish(pages, reference)
                        await checkpoint()
                        accepted = True
                        return receipt_page
                    if not isinstance(reference, str):
                        raise ValueError("Invalid provider reference.")
                    with self.service.store.source(reference) as opened:
                        size = os.fstat(opened).st_size
                        if size <= 0 or (
                            self.service.config.limits.source_bytes is not None
                            and size > self.service.config.limits.source_bytes
                        ):
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
            accepted = False
            return SelectionFailure.from_code("selection_timeout")
        except Exception:
            accepted = False
            return SelectionFailure.from_code("selection_failed")
        except BaseException:
            accepted = False
            raise
        finally:
            # Provider rollback owns copies; the coordinator owns only receipt pages.
            if pages is not None and not accepted:
                with anyio.CancelScope(shield=True), suppress(Exception):
                    await run_sync(pages.rollback)
            self.pending = False
