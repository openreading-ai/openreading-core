"""The four ports (internal/design/ledger.md §5.2): `Executor`, `Journal`, `BlobStore`, `KeyStore`,
each a `typing.Protocol`. Core reaches an executor only through the `Executor` Protocol on
`_WalkCtx` — no module under `router`/`strategies`/`batch` may name a concrete implementation or
`isinstance`-check one (§5.2's own pin; `strategies/engine.py`'s `_WalkCtx.executor` field is typed
`Executor`, never `InlineExecutor`).

`Executor.exec`'s `run` parameter is T1's own scoping decision. The design doc's canonical form is
`ctx.exec(StepRequest)` alone, appropriate for a substrate that reconstructs the call from the
request's slim projection. T1 ships only `InlineExecutor`, which runs in-process and therefore does
not reconstruct anything — it needs the caller's already-built closure over the real
adapter/request/broker to actually do the work; the `StepRequest` it receives is the audit-facing
shape, not the dispatch mechanism. A future distributed executor's `exec` would ignore `run` (or the
Protocol drops it once decomposition and reconstruction-from-request are real, in T3+) — named as an
interim shape, not a mock of the final one, matching the design doc's own framing of `ctx.exec`
throughout T1.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from openreading.ledger.descriptor import ExecutorDescriptor
from openreading.ledger.step import BlobRef, ExecResult, StepRef, StepRequest, StepResult
from openreading.types.errors import TerminalError


class LedgerArmingError(TerminalError):
    """`$OPENREADING_LEDGER` names a path this process cannot journal to.

    A `TerminalError` so every surface already handles it: the CLI's exit-3 "cannot run" rung with
    one tagged line, the server's coded error body. It is deliberately fatal — L1's zero-delta
    promise covers the UNARMED case only, and a run that cannot be journalled must not quietly
    proceed as if it were resumable — but the operator has to be told which knob did it. The raw
    `PermissionError`/`NotADirectoryError` carried an errno and a path and never the variable's
    name, arriving at the generic exit-1 "unexpected error" rung as though the parse had a bug.
    """

    def __init__(self, root: str, cause: OSError) -> None:
        super().__init__(
            f"cannot use the run journal at OPENREADING_LEDGER={root!r}: "
            f"{cause.strerror or cause}. Point it at a writable directory, or unset it to run "
            "without a journal (an unjournalled run is not resumable).",
            backend_code="ledger_unavailable",
        )
        self.root = root


class Executor(Protocol):
    """The one port the strategy walk dispatches through. `exec` journals the intent, runs the
    step, journals the outcome, and serves a recorded outcome back instead of re-running it."""

    @property
    def descriptor(self) -> ExecutorDescriptor: ...

    async def exec(self, req: StepRequest, *, run: Callable[[], Any]) -> ExecResult:
        """Ledger T3: journal an `attempted` record, invoke `run()`, journal the terminal record,
        and return an `ExecResult` wrapping whatever `run()` returned as `payload` (T1's own
        bare-payload return, additively wrapped — every existing "ok" path's payload is unchanged,
        just unpacked one level).

        A step already terminal in the journal (replay, §4.3) short-circuits before `run()` is ever
        called: a non-`"failed"` recorded status returns its recorded outcome as an `ExecResult`
        with `replayed=True`; a `"failed"` one reconstructs and re-raises the recorded error, the
        same as a live failure would. `ExecResult.status` is therefore only ever `"ok"`,
        `"skipped"`, or `"cancelled"` — a `"failed"` outcome, live or replayed, always raises
        instead of returning, so a caller's own `except (TerminalError, RetryableError,
        UnsupportedFeatureError, ComplianceRefused)` handling keeps working unmodified either way."""
        ...


class Journal(Protocol):
    """Append-only per-run log of `StepResult` records. `get` returns every record at one key, in
    append order."""

    def append(self, result: StepResult) -> StepResult:
        """Ledger T3: returns the same record, with `journal_seq` populated (a per-run monotonic
        append count) — additive to L1's original no-return contract; every existing caller that
        ignores the return value is unaffected. `NullJournal.append` returns its argument unchanged,
        `journal_seq` staying `None` (L1's zero-delta guarantee)."""
        ...

    def get(self, ref: StepRef) -> list[StepResult]:
        """Every record at `ref`, in append order, never a single value: a completed leaf's key
        holds `[attempted, <terminal>]`."""
        ...


class BlobStore(Protocol):
    """Content-addressed payload store keyed by `(run_id, digest)`. `get` raises `FileNotFoundError`
    once the run's key is gone."""

    def put(self, run_id: str, digest: str, data: bytes, media_type: str) -> BlobRef: ...

    def get(self, ref: BlobRef) -> bytes:
        """Raises `FileNotFoundError` when no blob was stored for this ref."""
        ...


class KeyStore(Protocol):
    """One encryption key per run. `destroy` is the erasure primitive the retention sweep
    calls."""

    def get_or_create(self, run_id: str) -> bytes: ...

    def get(self, run_id: str) -> bytes:
        """Raises `KeyError` (or a subclass) once the run's key is destroyed."""
        ...

    def destroy(self, run_id: str) -> None: ...
