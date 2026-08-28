"""Router: the driver (sync/async/webhook unifier), idempotency cache, and (0.6) the
3-stage compliance-first router."""

from __future__ import annotations

from openreading.router.cache import (
    InMemoryResultCache,
    ResultCache,
    content_key,
)
from openreading.router.clock import Clock, FakeClock, RealClock
from openreading.router.compliance import DropReason, RouterConfig
from openreading.router.driver import await_result, backoff_ms, run_to_completion
from openreading.router.registry import Registry
from openreading.router.router import RoutePlan, Router

__all__ = [
    "await_result",
    "run_to_completion",
    "backoff_ms",
    "Clock",
    "RealClock",
    "FakeClock",
    "content_key",
    "ResultCache",
    "InMemoryResultCache",
    "Registry",
    "Router",
    "RoutePlan",
    "RouterConfig",
    "DropReason",
]
