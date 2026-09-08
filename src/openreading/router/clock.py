"""Injectable clock so the poll/backoff driver is deterministic in tests.

The driver never calls time.monotonic()/asyncio.sleep() directly — it goes through a Clock,
so tests inject a FakeClock that advances virtual time without real waiting.

**Two clocks, and which one a value needs.** `now_ms()` is monotonic and `now_wall_ms()` is the
Unix epoch; they are not interchangeable and the choice is a correctness decision, not a style
one.

- `now_ms()` measures elapsed time *inside one process*: deadlines, retry backoff, cache TTLs,
  `duration_ms`. Wall time is wrong for all of these because it is adjustable — an NTP correction
  or a DST jump backwards silently extends every live deadline, and one forwards expires healthy
  runs at once. Monotonic time cannot be stepped, which is the whole reason it exists.
- `now_wall_ms()` is for any instant that **leaves the process**: written to disk, journaled, or
  read back by a later process. Monotonic time is wrong for all of these because its zero point
  is the boot (and Python leaves it formally undefined), so a monotonic reading is meaningless to
  anyone who did not observe the same boot. Persisting one and comparing it later is the defect
  this split exists to prevent: the ledger's retention stamp held `time.monotonic()` under a field
  named `expires_epoch_ms`, so it read as 1970 to a loader and, after a reboot, sat permanently in
  the future of a reaper whose own clock had restarted near zero — documents held past the window
  an operator had set. That retention machinery is gone (`CHANGELOG.md`, Unreleased), and the rule
  it taught is why this split stays.

The rule: a number you subtract from another reading of the same clock is `now_ms()`; a number
that names a moment to anyone else is `now_wall_ms()`.
"""

from __future__ import annotations

import asyncio
import heapq
import time
from typing import Protocol


class Clock(Protocol):
    """What the driver and the engine need from time: `now_ms()` for monotonic readings,
    `now_wall_ms()` for epoch readings, and `sleep()`. The module docstring says which one a
    given value needs."""

    def now_ms(self) -> float: ...
    def now_wall_ms(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class RealClock:
    """The production clock, backed by `time.monotonic`, `time.time` and `asyncio.sleep`."""

    def now_ms(self) -> float:
        return time.monotonic() * 1000.0

    def now_wall_ms(self) -> float:
        return time.time() * 1000.0

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class FakeClock:
    """Virtual clock: `sleep` advances `now` instead of blocking (fast, deterministic tests).

    Two modes:
    - **auto-advance** (default) — a single-task `sleep` bumps `now` immediately. This is what the
      serial POLL driver uses (one task at a time), unchanged from before.
    - **coordinated** (opt-in, for concurrent branches, harness H1) —
      `sleep` registers a wake-time and awaits a future; a driver calls `advance_to_next()` to move
      to the earliest pending wake time only when every runnable task is parked on `clock.sleep`.
      This makes a race between branches with different virtual latencies deterministic.

    **Not safe across an `asyncio.run()` boundary in a different thread** (Ledger T2):
    coordinated `sleep`'s future is bound to
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

    #: An arbitrary fixed instant (2023-11-14T22:13:20Z) so a FakeClock-driven run journals a
    #: plausible, deterministic wall-clock stamp instead of 1970.
    DEFAULT_WALL_START_MS = 1_700_000_000_000.0

    def __init__(self, start_ms: float = 0.0, wall_start_ms: float | None = None) -> None:
        self._now = start_ms
        self._mono_start = start_ms
        self._wall_start = (
            self.DEFAULT_WALL_START_MS if wall_start_ms is None else float(wall_start_ms)
        )
        self._coordinated = False
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._seq = 0

    def now_ms(self) -> float:
        return self._now

    def now_wall_ms(self) -> float:
        """Virtual wall time: the two bases share one origin and advance together, so a virtual
        sleep moves both. A reboot is modelled by constructing a NEW FakeClock — small
        `start_ms`, larger `wall_start_ms` — which is exactly the discontinuity a real one is."""
        return self._wall_start + (self._now - self._mono_start)

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
