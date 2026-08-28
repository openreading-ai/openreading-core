"""The driver is the entire concurrency surface. These tests drive each wait mode over one
Job state machine with a virtual clock (no real sleeping)."""

from __future__ import annotations

import pytest

from openreading.router import FakeClock, await_result, backoff_ms
from openreading.types import JobState
from openreading.types.errors import RetryableError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext
from tests.fakes import InlineFake, NeverFinishesFake, PollFake, WebhookFake

REQ = OpenReadingRequest.model_validate({"document": {"path": "/x.pdf"}, "backend": {"id": "fake"}})
CTX = RunContext()


async def test_inline_returns_immediately_without_entering_loop():
    a = InlineFake()
    job = a.submit(REQ, CTX)
    assert job.state is JobState.SUCCEEDED  # submit already completed it
    clock = FakeClock()
    # deadline already in the past — an INLINE job still returns (no work to wait on)
    out = await await_result(a, job, ctx=CTX, deadline_ms=-1.0, clock=clock)
    assert out.state is JobState.SUCCEEDED
    resp = a.normalize(out, CTX, REQ)
    assert resp.document.text == "inline result"


async def test_poll_transitions_running_to_succeeded():
    a = PollFake(polls_needed=3)
    clock = FakeClock()
    job = a.submit(REQ, CTX)
    assert job.state is JobState.RUNNING
    out = await await_result(a, job, ctx=CTX, deadline_ms=1e9, clock=clock)
    assert out.state is JobState.SUCCEEDED
    assert out.raw.payload == "poll result"


async def test_poll_retries_on_retryable_then_succeeds():
    a = PollFake(polls_needed=1, flaky=2)
    clock = FakeClock()
    job = a.submit(REQ, CTX)
    out = await await_result(a, job, ctx=CTX, deadline_ms=1e9, clock=clock)
    assert out.state is JobState.SUCCEEDED
    assert out.attempts >= 2  # counted the transient failures


async def test_deadline_exceeded_raises_retryable():
    a = NeverFinishesFake()
    clock = FakeClock(start_ms=0.0)
    job = a.submit(REQ, CTX)
    # deadline is soon; each poll pushes next_poll_at +1000ms so the clock crosses it fast
    with pytest.raises(RetryableError, match="deadline exceeded"):
        await await_result(a, job, ctx=CTX, deadline_ms=5000.0, clock=clock)


async def test_max_consecutive_faults_cap_is_enforced():
    a = PollFake(polls_needed=1, flaky=1000)  # always flaky
    clock = FakeClock()
    with pytest.raises(RetryableError):
        await await_result(
            a, a.submit(REQ, CTX), ctx=CTX, deadline_ms=1e18, clock=clock, max_consecutive_faults=5
        )


async def test_healthy_long_running_job_is_not_terminated_by_the_fault_counter():
    # BL-169 bug A: job.attempts only increments inside `except RetryableError` — a merely-slow,
    # HEALTHY job (never faults, just needs many polls) must not be bounded by
    # MAX_CONSECUTIVE_FAULTS at all, only by the absolute deadline. 200 clean polls is well past
    # the default cap (120) and would have raised under the old, misleadingly-named
    # MAX_POLL_ATTEMPTS if it had ever bounded total polls rather than consecutive faults.
    a = PollFake(polls_needed=200, flaky=0)
    clock = FakeClock()
    job = a.submit(REQ, CTX)
    out = await await_result(a, job, ctx=CTX, deadline_ms=1e9, clock=clock)
    assert out.state is JobState.SUCCEEDED
    assert out.attempts == 0  # never faulted, so the counter never engaged


async def test_webhook_wait_mode_degrades_straight_to_polling():
    # Ledger T4a: `WebhookBus` (the driver's own push-await mechanism) was deleted as genuinely
    # dead code — a repo-wide grep found zero production call sites ever passed a non-None
    # `webhook_bus`, matching the design doc's own scope-cut ledger ("every WEBHOOK job already
    # degrades to polling"). A WaitMode.WEBHOOK job now has no push-await path in the driver at
    # all; it goes straight to the same poll path every other wait mode uses. WebhookFake.poll()
    # is the inherited INLINE-default no-op (returns unchanged), so this job never progresses —
    # the deadline is what stops it, same shape as test_deadline_exceeded_raises_retryable above,
    # just proving the same thing for a WEBHOOK-wait-mode job.
    a = WebhookFake()
    clock = FakeClock(start_ms=0.0)
    with pytest.raises(RetryableError, match="deadline exceeded"):
        await await_result(a, a.submit(REQ, CTX), ctx=CTX, deadline_ms=2000.0, clock=clock)


def test_backoff_is_exponential_and_honors_retry_after():
    assert backoff_ms(1, None) == 500.0
    assert backoff_ms(2, None) == 1000.0
    assert backoff_ms(3, None) == 2000.0
    assert backoff_ms(1, retry_after=5.0) == 5000.0  # retry_after (5s) wins over 500ms
    assert backoff_ms(100, None) == 30_000.0  # capped
