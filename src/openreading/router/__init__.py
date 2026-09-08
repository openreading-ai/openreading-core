"""Resolve a request's default backend order and drive the winner to a result.

`Router.route(request)` returns a `RoutePlan`: a chosen backend, an ordered fallback list, and a
`DropReason` for every backend a later caller-scope restriction excludes.
`executor.execute_plan(plan, request)` walks that plan until a backend succeeds and returns one
`NormalizedResponse`. Nothing here branches on a backend's type.

Selection is a lookup with no inference in it. Three rules, in order, in
`openreading.router.router`:

1. **The backend the caller named.** A chain of one.
2. **Else `policy.backends`, in written order.** That list is the chain. An EMPTY list leaves no
   default backend and refuses with `scope_denied`; an absent list is not an empty one.
3. **Else `pymupdf`.** It needs no key and no config, so a fresh clone reads a document with no
   setup.

There used to be three stages here instead: a compliance hard-filter reading a twelve-field
profile off every descriptor, a capability gate reading `input_formats` and five feature flags,
and a scorer weighing "quality" against price. Every input to all three was a claim this package
could not verify, and being wrong did not fail loudly. It routed a document to a backend the
operator believed was excluded, and the run succeeded. The rule that replaced them, and the one
every module here now keeps: **core holds no fact it cannot verify. A constraint core cannot check
is a constraint core must not appear to enforce.**

Two kinds of statement, and they are not the same kind. `policy.backends` is the chain a request
that names NO backend resolves to, and `routing.fallback` reorders within it and never adds to it.
Naming a backend (`--backend reducto`, `backend.id`, a strategy step) is an explicit act by the
caller and runs that backend, list or no list: on one machine the operator and the caller are the
same person, and refusing what they just typed helps nobody. The BOUNDARY, where those are two
different people, is the server's API-key scope: a scoped key refuses a backend outside its scope
with `scope_denied` before any credential is resolved, whatever the body named.

A backend that cannot read a document refuses first-hand and the chain moves to the next one,
which costs one round trip.

Sibling modules: `driver` runs the one poll-and-backoff loop every wait mode shares, `cost`
turns an adapter's `CostReport` into `response.usage` by filling only the slots the adapter
left unset, `cache` derives the document identity and the idempotency key, `clock` keeps
monotonic time apart from wall time, and `registry` maps a descriptor id to its adapter.
`openreading.strategies` calls `Router.route` once per compile, for the chain it reports rather
than for a chain it dispatches from: every strategy leaf names its own backend. Server API-key
scope prunes every dispatch.
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
    "DropReason",
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
]
