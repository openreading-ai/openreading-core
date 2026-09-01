"""The driver is the entire concurrency surface. These tests drive each wait mode over one
Job state machine with a virtual clock (no real sleeping)."""

from __future__ import annotations

import pytest

from openreading.router import FakeClock, await_result, backoff_ms
from openreading.router.driver import MAX_CONSECUTIVE_FAULTS
from openreading.types import Job, JobState, WaitMode
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


async def test_poll_scheduled_past_deadline_never_polls():
    """A next_poll_at far beyond the deadline must expire the slice, not sleep-then-poll: an
    uncapped sleep lets a backend-scheduled (or retry_after-inflated) next_poll_at park the
    driver — and whatever thread/lock the caller tied to this slice — arbitrarily long (H4)."""
    clk = FakeClock(start_ms=0.0)
    polls = []

    class Adapter:
        def poll(self, job, ctx):
            polls.append(clk.now_ms())
            return job  # never reached if the fix holds

    job = Job(
        id="omjob_test",
        backend_id="never-fake",
        wait_mode=WaitMode.POLL,
        state=JobState.RUNNING,
        next_poll_at=60_000.0,
    )
    with pytest.raises(RetryableError):  # _DriveSliceExpired subclasses RetryableError
        await await_result(Adapter(), job, ctx=CTX, deadline_ms=1_000.0, clock=clk)
    assert polls == []  # zero polls after budget
    assert clk.now_ms() <= 1_000.0  # slept at most to the deadline, not to 60s


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


async def test_intermittent_faults_do_not_exhaust_a_long_running_healthy_job():
    # M8: `max_consecutive_faults` must bound a CONSECUTIVE fault streak, as its name promises —
    # not a lifetime total. Script 2 * MAX_CONSECUTIVE_FAULTS fault/healthy-poll pairs: cumulative
    # faults (2x the budget) would exhaust a lifetime counter, but no run of CONSECUTIVE faults
    # ever exceeds 1 because a healthy nonterminal poll always follows each fault. The job must
    # still reach SUCCEEDED — today (pre-fix) the cumulative count exhausts the budget partway
    # through and this raises instead.
    clk = FakeClock()
    total_cycles = 2 * MAX_CONSECUTIVE_FAULTS
    calls = {"n": 0}

    class AlternatingFaultAdapter:
        def poll(self, job, ctx):
            calls["n"] += 1
            cycle = (calls["n"] + 1) // 2  # calls 1&2 -> cycle 1, calls 3&4 -> cycle 2, ...
            if cycle > total_cycles:
                job.state = JobState.SUCCEEDED
                return job
            if calls["n"] % 2 == 1:  # first call of the cycle: a transient fault
                raise RetryableError("transient", retry_after=0.001)
            job.next_poll_at = clk.now_ms() + 1.0  # second call: healthy, still running
            return job

    job = Job(
        id="omjob_test",
        backend_id="alternating-fake",
        wait_mode=WaitMode.POLL,
        state=JobState.RUNNING,
        next_poll_at=0.0,
    )
    out = await await_result(AlternatingFaultAdapter(), job, ctx=CTX, deadline_ms=1e9, clock=clk)
    assert out.state is JobState.SUCCEEDED
    # every cycle made 2 calls (fault + healthy), plus the final call that returned success
    assert calls["n"] == 2 * total_cycles + 1
    # the streak was reset after every healthy poll, so it never carried past a single fault
    assert out.attempts == 0


async def test_max_consecutive_faults_plus_one_in_a_row_still_raises():
    # Regression guard for the fix above: the reset must fire ONLY on a healthy nonterminal poll.
    # MAX_CONSECUTIVE_FAULTS + 1 faults with NOTHING healthy between them is a genuine consecutive
    # exhaustion and must still raise -- the budget is consecutive, not disabled.
    a = PollFake(polls_needed=1, flaky=MAX_CONSECUTIVE_FAULTS + 1)
    clock = FakeClock()
    with pytest.raises(RetryableError):
        await await_result(a, a.submit(REQ, CTX), ctx=CTX, deadline_ms=1e18, clock=clock)


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


# ---- the two clock bases (B5) -----------------------------------------------------------------


def test_now_ms_is_monotonic_and_never_wall_clock():
    """`now_ms` measures elapsed time inside one process: deadlines, backoff, TTLs, durations.
    An NTP step or a DST jump must not move it, so it must NOT be `time.time()`."""
    import time

    from openreading.router.clock import RealClock

    reading = RealClock().now_ms()
    assert abs(reading - time.monotonic() * 1000.0) < 1000.0
    # A wall-clock epoch in ms is ~1.7e12; a monotonic reading is uptime, orders smaller.
    assert reading < time.time() * 1000.0 / 2


def test_now_wall_ms_is_an_absolute_utc_epoch():
    """`now_wall_ms` is the only clock whose reading may be written to disk or handed to a
    consumer: it is comparable across processes and across a reboot, which `now_ms` is not."""
    import time

    from openreading.router.clock import RealClock

    assert abs(RealClock().now_wall_ms() - time.time() * 1000.0) < 1000.0


def test_fake_clock_advances_both_bases_together():
    """A virtual sleep must move wall time too, or a FakeClock-driven test would journal the same
    timestamp for a step that took an hour of virtual time."""
    clock = FakeClock(start_ms=0.0, wall_start_ms=1_700_000_000_000.0)
    assert clock.now_wall_ms() == 1_700_000_000_000.0
    import asyncio

    asyncio.run(clock.sleep(90.0))
    assert clock.now_ms() == 90_000.0
    assert clock.now_wall_ms() == 1_700_000_090_000.0
