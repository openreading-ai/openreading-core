"""The four ports (internal/design/ledger.md §5.2): `Executor`, `Journal`, `BlobStore`, `KeyStore`,
each a `typing.Protocol`. Core reaches an executor only through the `Executor` Protocol on
`_WalkCtx` — no module under `router`/`strategies`/`batch` may name a concrete implementation or
`isinstance`-check one (§5.2's own pin; `strategies/engine.py`'s `_WalkCtx.executor` field is typed
`Executor`, never `InlineExecutor`).

`Executor.exec`'s `run` parameter is T1's own scoping decision (plan §10 leaves exact signatures to
Build): the design doc's canonical form is `ctx.exec(StepRequest)` alone, appropriate for a
substrate that reconstructs the call from the request's slim projection. T1 ships only
`InlineExecutor`, which runs in-process and therefore does not reconstruct anything — it needs the
caller's already-built closure over the real adapter/request/broker to actually do the work; the
`StepRequest` it receives is the audit-facing shape, not the dispatch mechanism. A future
distributed executor's `exec` would ignore `run` (or the Protocol drops it once decomposition and
reconstruction-from-request are real, in T3+) — named as an interim shape, not a mock of the final
one, matching the plan's own framing of `ctx.exec` throughout T1.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from openreading.ledger.descriptor import ExecutorDescriptor
from openreading.ledger.step import BlobRef, ExecResult, StepRef, StepRequest, StepResult


class PayloadExpired(Exception):
    """Raised by `BlobStore.get` when the run's key has been destroyed (`KeyStore.destroy`) —
    the journal record survives, the plaintext does not (§9.4's "third terminal state")."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"run {run_id!r}: key destroyed, payload unrecoverable")
        self.run_id = run_id


class Executor(Protocol):
    @property
    def descriptor(self) -> ExecutorDescriptor: ...

    async def exec(self, req: StepRequest, *, run: Callable[[], Any]) -> ExecResult:
        """Ledger T3 (§4.0, resolving round-2 F7): journal an `attempted` record, invoke `run()`,
        journal the terminal record, and return an `ExecResult` wrapping whatever `run()` returned
        as `payload` (T1's own bare-payload return, additively wrapped — every existing "ok" path's
        payload is unchanged, just unpacked one level).

        A step already terminal in the journal (replay, §4.3) short-circuits before `run()` is ever
        called: a non-`"failed"` recorded status returns its recorded outcome as an `ExecResult`
        with `replayed=True`; a `"failed"` one reconstructs and re-raises the recorded error, the
        same as a live failure would. `ExecResult.status` is therefore only ever `"ok"`,
        `"skipped"`, or `"cancelled"` — a `"failed"` outcome, live or replayed, always raises
        instead of returning, so a caller's own `except (TerminalError, RetryableError,
        UnsupportedFeatureError, ComplianceRefused)` handling keeps working unmodified either way."""
        ...


class Journal(Protocol):
    def append(self, result: StepResult) -> StepResult:
        """Ledger T3 (§4.0): returns the same record, with `journal_seq` populated (a per-run
        monotonic append count) — additive to L1's original no-return contract; every existing
        caller that ignores the return value is unaffected. `NullJournal.append` returns its
        argument unchanged, `journal_seq` staying `None` (L1's zero-delta guarantee)."""
        ...

    def get(self, ref: StepRef) -> list[StepResult]:
        """Every record at `ref`, in append order — never a single value (plan §8, resolving
        Phase A round-2 F1): a completed leaf's key holds `[attempted, <terminal>]`."""
        ...


class BlobStore(Protocol):
    def put(self, run_id: str, digest: str, data: bytes, media_type: str) -> BlobRef: ...

    def get(self, ref: BlobRef) -> bytes:
        """Raises `PayloadExpired` once the run's key is destroyed."""
        ...


class KeyStore(Protocol):
    def get_or_create(self, run_id: str) -> bytes: ...

    def get(self, run_id: str) -> bytes:
        """Raises `KeyError` (or a subclass) once the run's key is destroyed."""
        ...

    def destroy(self, run_id: str) -> None: ...
