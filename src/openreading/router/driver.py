"""The router driver — the ENTIRE concurrency surface for all four backend types
(adapter_interface.md §3). The router never writes backend-specific waiting code: it runs one
loop over the Job state machine while the adapter's wait_mode + poll()/resolve_webhook() supply
the mechanics.

INLINE backends never enter the loop (submit() already returned terminal). POLL backends are
polled with backoff honoring retry_after. WEBHOOK backends have no push-await mechanism here (the
dead `WebhookBus` machinery was deleted, Ledger T4a) — they degrade straight to polling, same as
they always did in production.
"""

from __future__ import annotations

import asyncio

from openreading.router.clock import Clock, RealClock
from openreading.types.errors import RetryableError
from openreading.types.job import Job
from openreading.types.runtime import RunContext

# BL-169: this bounds CONSECUTIVE FAULTS (job.attempts, incremented only inside `except
# RetryableError` below), not total polls — a merely-slow, healthy job that keeps returning
# RUNNING without ever faulting never increments it and is bounded only by the absolute
# deadline. Renamed from MAX_POLL_ATTEMPTS, which claimed a broader guarantee than the code (or
# the docs) ever delivered.
MAX_CONSECUTIVE_FAULTS = 120
_BASE_BACKOFF_MS = 500.0
_MAX_BACKOFF_MS = 30_000.0


class _DriveSliceExpired(RetryableError):
    """BL-88: raised ONLY by this module's own per-call deadline check below (never by an
    adapter's poll()) — a dedicated TYPE so a caller (server/app.py's get_job) can tell "this
    call's own drive-deadline slice elapsed" apart from a genuine backend-reported failure or a
    real MAX_CONSECUTIVE_FAULTS exhaustion, without relying on `backend_code` content. `backend_code`
    is ordinary adapter-writable free text with no uniqueness constraint — an adapter's own
    genuine vendor-deadline error is free to carry this identical "deadline_exceeded" sentinel
    (the openreading.strategies.model docstring's own classifier table names it as vocabulary adapters SHOULD
    prefer), so a string match alone can misclassify real exhaustion as a harmless slice expiry.
    The sentinel string itself is intentionally UNCHANGED (still set below): `strategies/
    engine.py`'s `classify_error` deliberately, correctly matches any `RetryableError`'s
    `backend_code` against `_TIMEOUT_CODES` origin-agnostically, and must keep doing so — this
    type only sharpens server/app.py's own narrower discriminator, it does not touch that."""


def backoff_ms(attempts: int, retry_after: float | None) -> float:
    """Exponential backoff; a backend-supplied retry_after (seconds) wins when larger."""
    computed = min(_BASE_BACKOFF_MS * (2 ** max(0, attempts - 1)), _MAX_BACKOFF_MS)
    if retry_after is not None:
        return max(computed, retry_after * 1000.0)
    return computed


async def await_result(
    adapter,
    job: Job,
    *,
    ctx: RunContext,
    deadline_ms: float,
    clock: Clock | None = None,
    max_consecutive_faults: int = MAX_CONSECUTIVE_FAULTS,
) -> Job:
    """Drive `job` to a terminal state. `deadline_ms` is an absolute time on `clock`'s scale.

    `ctx` (Ledger T4a): passed straight through to every `adapter.poll(job, ctx)` call below — a
    POLL adapter builds its own client from `ctx` rather than assuming one bound at submit() time
    survived on this instance, which is what lets a fresh, freshly-constructed instance resume
    driving the job (AC-5). A WEBHOOK-wait-mode job has no push-await mechanism here (that dead
    machinery — `WebhookBus` — was deleted per Ledger T4a; the design doc's own scope-cut ledger
    had already called it dead code with no live call site) — it degrades straight to polling
    below, same as it always did in production."""
    clk = clock or RealClock()

    # INLINE backends are already terminal; the loop body never runs.
    while not job.is_terminal():
        if clk.now_ms() > deadline_ms:
            # BL-88: raises the dedicated _DriveSliceExpired type (not a bare RetryableError) so
            # a caller (server/app.py's get_job) can tell "this call's own slice ran out" apart
            # from a genuine backend-reported failure or a re-raised MAX_CONSECUTIVE_FAULTS
            # exhaustion by TYPE — the former must not permanently latch a still-healthy job as
            # failed, and unlike a `backend_code` string match, a type can't collide with an
            # adapter's own genuine-exhaustion error that happens to carry the identical sentinel
            # below.
            raise _DriveSliceExpired(
                message="deadline exceeded", backend_code="deadline_exceeded", retry_after=None
            )

        # POLL path (a WEBHOOK-wait-mode job degrades straight to this — see docstring above).
        if job.next_poll_at is not None:
            wait_s = max(0.0, (job.next_poll_at - clk.now_ms()) / 1000.0)
            await clk.sleep(wait_s)

        try:
            job = adapter.poll(job, ctx)
        except RetryableError as e:
            job.attempts += 1
            job.next_poll_at = clk.now_ms() + backoff_ms(job.attempts, e.retry_after)
            if job.attempts > max_consecutive_faults:
                raise
        # TerminalError / UnsupportedFeatureError propagate out unchanged.

        # Progress guard: a still-running job MUST schedule a future poll. A misbehaving
        # adapter that leaves next_poll_at in the past would hot-spin (and, under a virtual
        # clock, never advance time toward the deadline), so floor it to one backoff interval.
        if not job.is_terminal() and (job.next_poll_at is None or job.next_poll_at <= clk.now_ms()):
            job.next_poll_at = clk.now_ms() + _BASE_BACKOFF_MS

    return job


def run_to_completion(
    adapter,
    job: Job,
    *,
    ctx: RunContext,
    deadline_ms: float | None = None,
    clock: Clock | None = None,
) -> Job:
    """Synchronous convenience wrapper (used by the CLI / simple callers). `ctx`: see
    `await_result`'s own docstring."""
    clk = clock or RealClock()
    dl = deadline_ms if deadline_ms is not None else clk.now_ms() + 10 * 60 * 1000.0
    return asyncio.run(await_result(adapter, job, ctx=ctx, deadline_ms=dl, clock=clk))
