"""Chain executor (GOAL2 7.2). Fallback becomes executable: chosen→fallbacks, skip on missing
credentials, fall through on failure, record the trail in warnings[], exhaust to PlanExhaustedError,
and an idempotency cache (bounded LRU + TTL). All offline via fake adapters + a real local one."""

from __future__ import annotations

import os

import pytest

from openreading.adapters.registry import make_adapter
from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.executor import BoundedResultCache, execute_plan
from openreading.router.router import RoutePlan
from openreading.types.errors import PlanExhaustedError, RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.response import NormalizedResponse
from tests.fakes import ConfigurableBackend, NeverFinishesFake, ScriptedBackend, make_backend

_EMPTY_BROKER = EnvCredentialBroker({})


class _FailingBackend(ConfigurableBackend):
    def __init__(self, backend_id: str, exc: Exception) -> None:
        super().__init__(make_backend(backend_id, local=True).descriptor)
        self._exc = exc

    def submit(self, req, ctx):
        raise self._exc


def _req(document=None):
    return OpenReadingRequest.model_validate(
        {"document": document or {"path": "/d.pdf"}, "backend": {"id": "auto"}}
    )


def _file_req(path):
    return _req({"path": str(path)})


def _codes(resp: NormalizedResponse) -> list[str]:
    return [w.code for w in (resp.warnings or [])]


# --- fallback on failure ---------------------------------------------------------------


def test_falls_back_on_terminal_and_records_trail():
    plan = RoutePlan(
        chosen=_FailingBackend("bad", TerminalError("boom", backend_code="x")),
        fallbacks=[make_backend("good", local=True)],
    )
    resp = execute_plan(plan, _req(), broker=_EMPTY_BROKER)
    assert resp.backend.id == "good"
    assert "fallback_used" in _codes(resp)
    assert any((w.field == "bad") for w in resp.warnings)


def test_falls_back_on_retryable_exhausted():
    plan = RoutePlan(
        chosen=_FailingBackend("flaky", RetryableError("throttled", backend_code="429")),
        fallbacks=[make_backend("good", local=True)],
    )
    resp = execute_plan(plan, _req(), broker=_EMPTY_BROKER)
    assert resp.backend.id == "good"


def test_falls_back_on_a_plain_normalize_crash():
    # BL-99: adapter.normalize() is ordinary adapter code, not one of the five _ADAPTER_ERRORS
    # taxonomy types — a plain KeyError/IndexError/ValueError/AttributeError out of it used to
    # propagate straight out of execute_plan uncaught, past even a healthy second backend, directly
    # contradicting this module's own docstring ("Terminal / Retryable-exhausted / UnsupportedFeature
    # fall to the next backend"). ScriptedBackend (not _FailingBackend, whose submit() raises)
    # accepts the job and only crashes at normalize() — the only way to reach this call site at all.
    good = ScriptedBackend("good", local=True, text="clean fallback text " * 5)
    bad = ScriptedBackend("bad", normalize_error=ValueError("malformed page structure"))
    plan = RoutePlan(chosen=bad, fallbacks=[good])
    resp = execute_plan(plan, _req(), broker=_EMPTY_BROKER)
    assert resp.backend.id == "good"
    assert len(good.contexts) == 1  # the second backend was actually contacted, not just returned
    assert "fallback_used" in _codes(resp)


# --- deadline_ms budget (BL-90) ---------------------------------------------------------


def test_deadline_ms_zero_fails_fast_not_full_default_budget():
    """`deadline_ms=0` is a caller stating "no budget left" — it must fail fast, not be silently
    upgraded to the full DEFAULT_DEADLINE_MS by `0 or ctx.deadline_ms or DEFAULT_DEADLINE_MS`
    truthiness (BL-90, the same bug shape BL-22 fixed in `Router._score()`'s cost-bound
    comparison). A backend that never completes makes the two outcomes distinguishable: with the
    budget correctly computed as `0`, the virtual clock advances only a single poll interval
    before the deadline is exceeded; a truthiness bug would silently grant the full 120s default
    and the clock would advance close to that instead."""
    clock = FakeClock()
    plan = RoutePlan(chosen=NeverFinishesFake())
    with pytest.raises(PlanExhaustedError) as exc:
        execute_plan(plan, _req(), broker=_EMPTY_BROKER, clock=clock, deadline_ms=0)
    assert exc.value.trail[0]["category"] == "RetryableError"
    # Far short of the ~120s DEFAULT_DEADLINE_MS a truthiness bug would silently grant instead.
    assert clock.now_ms() < 5_000


# --- skip on missing credentials -------------------------------------------------------


def test_skips_backend_missing_credentials(monkeypatch):
    monkeypatch.delenv("REDUCTO_API_KEY", raising=False)
    plan = RoutePlan(
        chosen=make_adapter("reducto"),  # required api_key, none in the empty broker → skipped
        fallbacks=[make_backend("good", local=True)],
    )
    resp = execute_plan(plan, _req(), broker=_EMPTY_BROKER)
    assert resp.backend.id == "good"
    assert any(w.code == "fallback_used" and w.field == "reducto" for w in resp.warnings)


# --- exhaustion ------------------------------------------------------------------------


def test_plan_exhausted_raises_with_full_trail():
    plan = RoutePlan(
        chosen=_FailingBackend("a", TerminalError("boom-a")),
        fallbacks=[_FailingBackend("b", TerminalError("boom-b"))],
    )
    with pytest.raises(PlanExhaustedError) as exc:
        execute_plan(plan, _req(), broker=_EMPTY_BROKER)
    trail = exc.value.trail
    assert [t["backend"] for t in trail] == ["a", "b"]
    assert exc.value.backend_code == "plan_exhausted"


def test_no_eligible_backend_run_when_only_skipped(monkeypatch):
    monkeypatch.delenv("REDUCTO_API_KEY", raising=False)
    # a plan of only a missing-creds hosted backend must NOT reach for anything outside the plan
    plan = RoutePlan(chosen=make_adapter("reducto"), fallbacks=[])
    with pytest.raises(PlanExhaustedError) as exc:
        execute_plan(plan, _req(), broker=_EMPTY_BROKER)
    assert exc.value.trail[0]["category"] == "skipped"


# --- deadline_ms propagation (BL-146) ---------------------------------------------------


@pytest.mark.parametrize("deadline_ms", [1000, 0])
def test_execute_plan_threads_deadline_ms_into_run_context(deadline_ms):
    # RunContext.deadline_ms is the field an adapter's own code actually reads (e.g.
    # TesseractAdapter.submit()'s subprocess timeout) — a caller's explicit deadline_ms (tight or
    # an explicit zero) must reach it, not leave it silently pinned to DEFAULT_DEADLINE_MS.
    backend = ScriptedBackend("good", local=True)
    plan = RoutePlan(chosen=backend)
    execute_plan(plan, _req(), broker=_EMPTY_BROKER, deadline_ms=deadline_ms)
    assert backend.contexts[-1].deadline_ms == deadline_ms


# --- idempotency cache -----------------------------------------------------------------


def test_cache_hit_replays_with_warning(tmp_path):
    doc = tmp_path / "d.pdf"
    doc.write_bytes(b"one")
    cache = BoundedResultCache()
    clock = FakeClock()
    plan = RoutePlan(chosen=make_backend("good", local=True))
    r1 = execute_plan(plan, _file_req(doc), broker=_EMPTY_BROKER, cache=cache, clock=clock)
    r2 = execute_plan(plan, _file_req(doc), broker=_EMPTY_BROKER, cache=cache, clock=clock)
    assert "idempotent_replay" not in _codes(r1)  # first populated the cache
    assert "idempotent_replay" in _codes(r2)  # second was a replay
    assert r2.backend.id == "good"


@pytest.mark.parametrize(
    ("content", "mtime_ns"),
    [
        (b"much longer content", 1_000),  # size differs, mtime pinned identical
        (b"two", 2_000),  # size identical, mtime differs
    ],
)
def test_changed_file_at_the_same_path_is_a_cache_miss(tmp_path, content, mtime_ns):
    """The key is CONTENT, not the locator. Both halves of the local-file identity are load-bearing,
    so each is varied alone with the other pinned — the path never changes."""
    doc = tmp_path / "d.pdf"
    doc.write_bytes(b"one")
    os.utime(doc, ns=(1_000, 1_000))
    cache = BoundedResultCache()
    clock = FakeClock()
    plan = RoutePlan(chosen=make_backend("good", local=True))

    execute_plan(plan, _file_req(doc), broker=_EMPTY_BROKER, cache=cache, clock=clock)
    assert "idempotent_replay" in _codes(
        execute_plan(plan, _file_req(doc), broker=_EMPTY_BROKER, cache=cache, clock=clock)
    )

    doc.write_bytes(content)
    os.utime(doc, ns=(mtime_ns, mtime_ns))
    assert "idempotent_replay" not in _codes(
        execute_plan(plan, _file_req(doc), broker=_EMPTY_BROKER, cache=cache, clock=clock)
    )


@pytest.mark.parametrize(
    "document",
    [{"url": "https://example.com/d.pdf"}, {"file_id": "prov_123"}, {"path": "/does/not/exist"}],
)
def test_bare_locators_are_never_cached(document):
    """A locator can serve different bytes tomorrow, so it has no content identity — a miss every
    time is correct; a hit would be a stale result."""
    cache = BoundedResultCache()
    clock = FakeClock()
    plan = RoutePlan(chosen=make_backend("good", local=True))
    execute_plan(plan, _req(document), broker=_EMPTY_BROKER, cache=cache, clock=clock)
    r2 = execute_plan(plan, _req(document), broker=_EMPTY_BROKER, cache=cache, clock=clock)
    assert "idempotent_replay" not in _codes(r2)


def test_bounded_cache_ttl_expiry():
    cache = BoundedResultCache(ttl_ms=1000)
    resp = NormalizedResponse.model_validate(
        {
            "status": {"state": "succeeded"},
            "backend": {"id": "x", "type": "oss_library"},
            "document": {"text": "t"},
        }
    )
    cache.put("k", resp, now_ms=0)
    assert cache.get("k", now_ms=500) is not None
    assert cache.get("k", now_ms=1500) is None  # expired


def test_bounded_cache_lru_eviction():
    cache = BoundedResultCache(max_entries=2)
    r = NormalizedResponse.model_validate(
        {
            "status": {"state": "succeeded"},
            "backend": {"id": "x", "type": "oss_library"},
            "document": {"text": "t"},
        }
    )
    cache.put("a", r, now_ms=0)
    cache.put("b", r, now_ms=0)
    cache.put("c", r, now_ms=0)  # evicts "a" (oldest)
    assert cache.get("a", now_ms=0) is None
    assert cache.get("b", now_ms=0) is not None and cache.get("c", now_ms=0) is not None
