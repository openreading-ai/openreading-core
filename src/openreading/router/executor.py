"""Chain executor (GOAL2 milestone 7.2) — the piece that makes fallback EXECUTABLE, not just
demonstrable. `execute_plan` walks a RoutePlan chosen→fallbacks and returns the first successful
NormalizedResponse.

Invariants:
- It consumes ONLY the RoutePlan. It can never widen eligibility — the router already enforced
  compliance (never-relaxed, fail-closed), so every backend here is already eligible.
- A backend whose required credentials the broker cannot resolve is SKIPPED (never a crash, never
  a network preflight — the first real proof of a key is the submit call).
- Terminal / Retryable-exhausted / UnsupportedFeature fall to the next backend. (RetryableError
  reaching here means the driver already exhausted same-backend backoff.)
- The successful response is metered: `report_cost()` fills the `usage` fields the adapter left
  unset (`cost_usd` above all). A meter that raises degrades to a warning, never a failed run.
- The successful response records the attempt trail in warnings[] (`fallback_used`), plus any
  operator confirmation the responding backend's compliance eligibility rests on.
- Chain exhausted → PlanExhaustedError carrying the full trail.
- Idempotency cache (bounded LRU + TTL) is consulted before submit and populated after success; a
  cache hit adds an `idempotent_replay` warning. Keyed by CONTENT, never by secrets and never by a
  bare locator — a document the caller only named (url / file_id) is never cached (D-v3-3).
"""

from __future__ import annotations

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
from openreading.router.compliance import BAA_TIER_CONFIRMED_WARNING
from openreading.router.cost import apply_cost_report
from openreading.router.driver import run_to_completion
from openreading.router.router import RoutePlan
from openreading.types.errors import (
    ComplianceRefused,
    PlanExhaustedError,
    RetryableError,
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
_TAXONOMY = (TerminalError, RetryableError, UnsupportedFeatureError, ComplianceRefused)


@dataclass
class Attempt:
    backend: str
    category: (
        str  # "skipped" | "TerminalError" | "RetryableError" | "UnsupportedFeatureError" | ...
    )
    code: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"backend": self.backend, "category": self.category, "code": self.code}


class BoundedResultCache:
    """Process-local idempotency cache: bounded (LRU) with a TTL. `now_ms` is passed by the caller
    so expiry is deterministic under the injected clock (no wall-clock reads)."""

    def __init__(self, max_entries: int = _CACHE_MAX_ENTRIES, ttl_ms: int = _CACHE_TTL_MS) -> None:
        self._d: OrderedDict[str, tuple[float, NormalizedResponse]] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl_ms

    def get(self, key: str, now_ms: float) -> NormalizedResponse | None:
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


def _record_confirmations(resp: NormalizedResponse, plan: RoutePlan, chosen: str) -> None:
    note = plan.baa_tier_notes.get(chosen)
    if note is not None:
        resp.add_warning(BAA_TIER_CONFIRMED_WARNING, note, chosen)


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
                _record_confirmations(resp, plan, desc.id)
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
            # BL-99: adapter.normalize() is ordinary adapter code, not one of the four _TAXONOMY
            # types above — a plain KeyError/IndexError/ValueError/AttributeError out of it used to
            # propagate straight out of this function uncaught, past a healthy fallback backend,
            # contradicting this module's own docstring ("Terminal / Retryable-exhausted /
            # UnsupportedFeature fall to the next backend"). Treated as a terminal failure for THIS
            # backend (fall back, never retried) — the same fallback behavior _TAXONOMY gets.
            # execute_plan has never routed through readiness.auth_hinted (it predates it, BL-37) —
            # its own redaction has always been this hand-copied sequence, so the identical redact()
            # call is mirrored here by hand rather than delegated.
            e.args = (redact(str(e), secret_values(desc, ctx.credentials)),)
            trail.append(Attempt(desc.id, "terminal", "", str(e)))
            continue

        if cache is not None and key is not None:
            cache.put(key, resp, clock.now_ms())
        _record_confirmations(resp, plan, desc.id)
        _record_trail(resp, trail, desc.id)
        return resp

    raise PlanExhaustedError(
        f"all {len(plan.chain)} eligible backend(s) failed or were skipped",
        trail=[a.as_dict() for a in trail],
    )
