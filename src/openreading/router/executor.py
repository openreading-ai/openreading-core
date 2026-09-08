"""Chain executor: the piece that makes fallback EXECUTABLE, not just
demonstrable. `execute_plan` walks a RoutePlan chosen→fallbacks and returns the first successful
NormalizedResponse.

Invariants:
- It consumes only the `RoutePlan`, so the executor cannot invent another backend.
- A plan carrying a caller allow-list (`RoutePlan.backend_allowlist`, set by `restrict_to`) has
  already had the chain pruned to it. Every chain member is re-checked against it here anyway,
  before the run context that resolves the vendor credential is built, and an out-of-scope member
  raises ScopeRefused rather than being skipped: reaching that state means a layer above failed,
  and this is a security control, so it fails closed and loudly.
- A backend whose required credentials the broker cannot resolve is SKIPPED (never a crash, never
  a network preflight — the first real proof of a key is the submit call).
- Terminal / Retryable-exhausted / UnsupportedFeature fall to the next backend. (RetryableError
  reaching here means the driver already exhausted same-backend backoff.)
- The successful response is metered: `report_cost()` fills the `usage` counters the adapter left
  unset. A meter that raises degrades to a warning, never a failed run.
- The successful response records the attempt trail in `warnings[]` (`fallback_used`).
- Chain exhausted → PlanExhaustedError carrying the full trail.
- Idempotency cache (bounded LRU + TTL) is consulted before submit and populated after success; a
  cache hit adds an `idempotent_replay` warning. Keyed by CONTENT, never by secrets and never by a
  bare locator — a document the caller only named (url / file_id) is never cached (D-v3-3).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from openreading.credentials import (
    DEFAULT_DEADLINE_MS,
    EnvCredentialBroker,
    build_run_context,
    redact,
    secret_values,
)
from openreading.ledger.header import slim_request
from openreading.readiness import attach_auth_hint, missing_required
from openreading.router.cache import content_key, document_identity
from openreading.router.clock import Clock, RealClock
from openreading.router.cost import apply_cost_report
from openreading.router.driver import run_to_completion
from openreading.router.router import RoutePlan
from openreading.types.errors import (
    PlanExhaustedError,
    RetryableError,
    ScopeRefused,
    TerminalError,
    UnsupportedFeatureError,
)
from openreading.types.request import OpenReadingRequest
from openreading.types.response import NormalizedResponse

_CACHE_MAX_ENTRIES = 256
_CACHE_TTL_MS = 15 * 60 * 1000

# The four-category taxonomy `Attempt.category` promises (its own type comment below). Walked by
# isinstance, not `type(e).__name__`: a private subtype raised internally by a lower layer for its
# own dispatch purposes (e.g. driver.py's `_DriveSliceExpired(RetryableError)`, BL-88) must still
# report as its taxonomy base here, never leak its own concrete class name into this trail.
_TAXONOMY = (TerminalError, RetryableError, UnsupportedFeatureError, ScopeRefused)


@dataclass
class Attempt:
    backend: str
    # "skipped" | "terminal" (a crash outside the taxonomy) | "TerminalError" |
    # "RetryableError" | "UnsupportedFeatureError" | "ScopeRefused"
    category: str
    code: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"backend": self.backend, "category": self.category, "code": self.code}


class BoundedResultCache:
    """Process-local idempotency cache: bounded (LRU) with a TTL. `now_ms` is passed by the caller
    so expiry is deterministic under the injected clock (no wall-clock reads).

    `execute_plan` runs under `run_in_threadpool` (BL-146's server path dispatches concurrent
    requests to worker threads sharing one cache instance), and every method here is a
    read-modify-write over the same `OrderedDict` (`get`'s expiry path does a lookup then a
    `del`; `put` does an insert then a size-bounded eviction loop). Two threads racing those
    compound steps — e.g. both `get`s seeing the same expired key, both taking the `del` branch —
    turn a `dict.__delitem__` into a `KeyError` that propagates out of the cache as a 500, so the
    whole body of `get` and `put` is one atomic section under `self._lock`.
    """

    def __init__(self, max_entries: int = _CACHE_MAX_ENTRIES, ttl_ms: int = _CACHE_TTL_MS) -> None:
        self._d: OrderedDict[str, tuple[float, NormalizedResponse]] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_ms
        self._lock = threading.Lock()

    def get(self, key: str, now_ms: float) -> NormalizedResponse | None:
        with self._lock:
            item = self._d.get(key)
            if item is None:
                return None
            expires, value = item
            if now_ms >= expires:
                del self._d[key]
                return None
            self._d.move_to_end(key)
            return value

    def put(self, key: str, value: NormalizedResponse, now_ms: float) -> None:
        with self._lock:
            self._d[key] = (now_ms + self._ttl, value)
            self._d.move_to_end(key)
            while len(self._d) > self._max:
                self._d.popitem(last=False)


def _cache_key(req: OpenReadingRequest, adapter) -> str | None:
    identity = document_identity(req.document)
    if identity is None:
        return None
    version = adapter.descriptor.runtime.version_pin or "" if adapter.descriptor.runtime else ""
    return content_key(identity, adapter.descriptor.id, version, req.to_schema_dict())


def _record_trail(resp: NormalizedResponse, trail: list[Attempt], chosen: str) -> None:
    for a in trail:
        code = f" ({a.code})" if a.code else ""
        resp.add_warning(
            "fallback_used", f"{a.backend} {a.category}{code} → fell back to {chosen}", a.backend
        )


def execute_plan(
    plan: RoutePlan,
    req: OpenReadingRequest,
    *,
    broker: EnvCredentialBroker | None = None,
    clock: Clock | None = None,
    cache: BoundedResultCache | None = None,
    deadline_ms: int | None = None,
) -> NormalizedResponse:
    """Run the plan's chosen→fallbacks until one succeeds. Raises PlanExhaustedError (with the
    full attempt trail) if every backend fails or is skipped for missing credentials."""
    broker = broker or EnvCredentialBroker()
    clock = clock or RealClock()
    trail: list[Attempt] = []

    for adapter in plan.chain:
        desc = adapter.descriptor
        # The caller allow-list backstop, redundant with RoutePlan.restrict_to and here anyway.
        # This is the last point at which the backend is known and nothing has been built yet:
        # build_run_context below RESOLVES the vendor credential, and the allow-list exists
        # precisely so an out-of-scope backend never gets that far. Deliberately outside the try
        # below — ScopeRefused is not one of the four _TAXONOMY types, so raising it in there
        # would be swallowed by the `except Exception` fallback handler and become a trail entry,
        # turning the refusal into a silent skip to the next rung.
        #
        # Raise rather than skip: reaching here at all means the prune above did not run or did
        # not cover this path, and the cost of the two layers disagreeing is asymmetric — a
        # spurious refusal is a support ticket, a missed one spends someone else's vendor credits
        # and looks exactly like ordinary traffic. Mirrors strategies.engine._resolve_backend,
        # which guards the strategy walk's dispatch the same way for the same reason.
        if plan.backend_allowlist is not None and desc.id not in plan.backend_allowlist:
            raise ScopeRefused(
                f"this API key is not scoped to reach backend {desc.id!r}",
                backend_code=desc.id,
            )
        # BL-146: the caller's own deadline_ms must reach ctx.deadline_ms — the field an adapter's
        # own code actually reads (e.g. TesseractAdapter.submit()'s subprocess timeout) — not just
        # the local `budget` variable below (which only bounds this function's own wait-loop).
        ctx = build_run_context(req, desc, broker=broker, deadline_ms=deadline_ms)

        missing = missing_required(desc, ctx)
        if missing:
            trail.append(Attempt(desc.id, "skipped", "missing_credentials", ",".join(missing)))
            continue

        key = _cache_key(req, adapter) if cache is not None else None
        if cache is not None and key is not None:
            hit = cache.get(key, clock.now_ms())
            if hit is not None:
                resp = hit.model_copy(deep=True)
                resp.add_warning(
                    "idempotent_replay", f"replayed cached result for {desc.id}", desc.id
                )
                _record_trail(resp, trail, desc.id)
                return resp

        try:
            budget = (
                deadline_ms
                if deadline_ms is not None
                else ctx.deadline_ms
                if ctx.deadline_ms is not None
                else DEFAULT_DEADLINE_MS
            )
            job = adapter.submit(req, ctx)
            job = run_to_completion(
                adapter, job, ctx=ctx, deadline_ms=clock.now_ms() + budget, clock=clock
            )
            resp = apply_cost_report(
                adapter, job, adapter.normalize(job, ctx, slim_request(req)), ctx.credentials
            )
        except _TAXONOMY as e:
            attach_auth_hint(e, desc)  # a rejected key names its env var in the recorded detail
            # Belt-and-suspenders beyond auth_rejected: any OTHER secret the broker resolved for
            # this backend never survives in the failure's message either (BL-37).
            e.message = redact(e.message, secret_values(desc, ctx.credentials))
            e.args = (e.message,)
            code = getattr(e, "backend_code", None) or getattr(e, "feature", "") or ""
            category = next(cls.__name__ for cls in _TAXONOMY if isinstance(e, cls))
            trail.append(Attempt(desc.id, category, code, str(e)))
            continue
        except Exception as e:
            # BL-99: adapter.normalize() is ordinary adapter code, not one of the four
            # _TAXONOMY types above. A plain KeyError/IndexError/ValueError/AttributeError out
            # of it must fall to the next backend exactly like a TerminalError, never escape
            # this function past a healthy fallback. It is recorded as "terminal" so the
            # trail shows that the class is outside the taxonomy. execute_plan does not route
            # through readiness.auth_hinted (BL-37), so the same redact() call is mirrored
            # here by hand.
            e.args = (redact(str(e), secret_values(desc, ctx.credentials)),)
            trail.append(Attempt(desc.id, "terminal", "", str(e)))
            continue

        if cache is not None and key is not None:
            # A deep copy, because the two _record_* calls below mutate `resp` with THIS
            # request's confirmations/trail — the cached entry must stay the adapter's
            # canonical answer, or every later hit replays this caller's transient history.
            cache.put(key, resp.model_copy(deep=True), clock.now_ms())
        _record_trail(resp, trail, desc.id)
        return resp

    raise PlanExhaustedError(
        f"all {len(plan.chain)} eligible backend(s) failed or were skipped",
        trail=[a.as_dict() for a in trail],
    )
