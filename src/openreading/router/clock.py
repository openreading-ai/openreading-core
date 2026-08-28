"""Injectable clock so the poll/backoff driver is deterministic in tests.

The driver never calls time.monotonic()/asyncio.sleep() directly — it goes through a Clock,
so tests inject a FakeClock that advances virtual time without real waiting.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now_ms(self) -> float:
        return time.monotonic() * 1000.0

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class FakeClock:
    """Virtual clock: `sleep` advances `now` instead of blocking (fast, deterministic tests).

    Two modes:
    - **auto-advance** (default) — a single-task `sleep` bumps `now` immediately. This is what the
      serial POLL driver uses (one task at a time), unchanged from before.
    - **coordinated** (opt-in, for concurrent branches — harness H1 / integration.md §6 T3) —
      `sleep` registers a wake-time and awaits a future; a driver calls `advance_to_next()` to move
      to the earliest pending wake time only when every runnable task is parked on `clock.sleep`.
      This makes a race between branches with different virtual latencies deterministic.

    **Not safe across an `asyncio.run()` boundary in a different thread** (Ledger T2, disclosed in
    FOUNDER-INBOX.md 2026-08-22): coordinated `sleep`'s future is bound to
    `asyncio.get_running_loop()` at call time. `strategies/engine.py`'s `_run_branch` dispatches a
    POLL-mode leaf's driver loop via `asyncio.to_thread(...)` into `router/driver.py`'s
    `run_to_completion`, which wraps its own poll loop in a **fresh** `asyncio.run(...)` — a
    different loop than the one `_eval_parallel`'s own `advance_to_next()` resolves against. A
    coordinated `FakeClock` backing a real (non-INLINE) branch that needs an actual backoff sleep
    inside that boundary deadlocks: the future is never resolved by the right loop. INLINE-mode
    fakes never hit this (their driver loop never calls `sleep` for a real wait); production never
    hits this (`RealClock.sleep` has no shared future state to bind to the wrong loop). Fixing this
    for real needs a thread/loop-safe coordination primitive (e.g. `threading.Event`-backed, not
    `asyncio.Future`-backed) — out of scope for the unit that discovered it.
    """

    def __init__(self, start_ms: float = 0.0) -> None:
        self._now = start_ms
        self._coordinated = False
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._seq = 0

    def now_ms(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)  # yield so concurrent branches interleave
            return
        if not self._coordinated:
            self._now += seconds * 1000.0
            return
        wake = self._now + seconds * 1000.0
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (wake, self._seq, fut))
        self._seq += 1
        await fut

    # ---- coordinated-mode driver hooks ----
    def enable_coordination(self) -> None:
        self._coordinated = True

    def disable_coordination(self) -> None:
        self._coordinated = False

    def pending_sleepers(self) -> int:
        return len(self._sleepers)

    def next_wake_ms(self) -> float | None:
        """The earliest pending wake time (for a driver bounding an advance by a deadline), or None
        when nothing is parked."""
        return self._sleepers[0][0] if self._sleepers else None

    def advance_to_next(self) -> bool:
        """Advance to the earliest pending wake time and resolve every sleeper due at it. Returns
        False when there is nothing to advance."""
        if not self._sleepers:
            return False
        wake = self._sleepers[0][0]
        self._now = wake
        while self._sleepers and self._sleepers[0][0] <= wake:
            _, _, fut = heapq.heappop(self._sleepers)
            if not fut.done():
                fut.set_result(None)
        return True
