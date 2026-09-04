"""Choose which backends may run a request, in what order, and drive the winner to a result.

`Router.route(request)` returns a `RoutePlan`: a chosen backend, an ordered fallback list, and
one `DropReason` for every backend it excluded. `executor.execute_plan(plan, request)` walks
that plan until a backend succeeds and returns one `NormalizedResponse`. Nothing here branches
on a backend's type, because every decision comes from the static `AdapterDescriptor` an
adapter publishes.

The three stages live in `openreading.router.router`.

1. Compliance (`openreading.router.compliance`) is a hard filter. A request field such as
   `require_local`, or `require_baa` (a Business Associate Agreement is the contract a US
   health provider signs before a vendor may handle patient data), drops every backend whose
   descriptor does not prove that property. An unverified claim fails closed unless the
   operator sets `RouterConfig.allow_unverified_compliance`.
2. Capability is a boolean gate over the requested features, the extraction schema, and the
   document's input format.
3. Scoring reorders the survivors by quality, cost, and locality, and it re-admits nobody.

The rule every module here keeps: compliance is never relaxed by fallback. The fallback chain
is drawn only from the backends that survived stages 1 and 2. A fallback reaching past them
would hand patient data to a backend with no BAA, and nothing in the response would say so. An
empty survivor set is a `no_compliant_backend` refusal, never a quiet downgrade.

Sibling modules: `driver` runs the one poll-and-backoff loop every wait mode shares, `cost`
turns an adapter's `CostReport` into `response.usage` by filling only the slots the adapter
left unset, `cache` derives the document identity and the idempotency key, `clock` keeps
monotonic time apart from wall time, and `registry` maps a descriptor id to its adapter.
`openreading.strategies` calls `Router.route` once when it compiles a strategy tree and prunes
that tree to the survivors, and it never widens them.
"""

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
