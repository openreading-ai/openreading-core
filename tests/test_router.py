"""The router resolves a chain. It does not filter and it does not score.

This file used to test three stages: a compliance hard-filter over a per-vendor table, a
capability gate over `input_formats` and five `Features` flags, and a cost/quality scorer. All
three are gone (`design/compliance-removal.md`, `design/explicit-backends.md`), and with them the
twenty-odd tests that pinned their worked examples.

What remains is small on purpose. Selection is the backend the caller named, else
`policy.backends` in order, else `pymupdf`, and `tests/test_explicit_backends.py` owns those three
rules. This file covers the one piece of ordering logic the router still performs: honouring a
caller's `routing.fallback` within the resolved set.
"""

from __future__ import annotations

from openreading.router.compliance import RouterConfig
from openreading.router.registry import Registry
from openreading.router.router import Router
from openreading.types.request import OpenReadingRequest
from tests.fakes import make_backend


def _registry(*ids: str) -> Registry:
    reg = Registry()
    for i in ids:
        reg.register(make_backend(i, local=True))
    return reg


def _req(**kw) -> OpenReadingRequest:
    body = {"document": {"bytes_base64": "eA=="}, "backend": {"id": None}}
    body.update(kw)
    return OpenReadingRequest.model_validate(body)


def test_the_chain_is_the_allow_list_in_written_order():
    reg = _registry("a", "b", "c")
    plan = Router(reg, RouterConfig(backends=("c", "a"))).route(_req())
    assert plan.eligible_ids == ["c", "a"]


def test_a_named_backend_is_a_chain_of_one():
    reg = _registry("a", "b")
    plan = Router(reg).route(_req(backend={"id": "b"}))
    assert plan.eligible_ids == ["b"]


def test_explicit_fallback_reorders_and_dedupes_within_the_resolved_set():
    """`routing.fallback` moves listed ids to the front in the caller's order and de-duplicates
    them. It reorders a restriction; it never widens one, so an id outside the resolved set is
    ignored rather than added."""
    reg = _registry("a", "b", "c")
    plan = Router(reg, RouterConfig(backends=("a", "b", "c"))).route(
        _req(routing={"fallback": ["c", "c", "zzz"]})
    )
    assert plan.eligible_ids == ["c", "a", "b"]


def test_an_id_naming_no_registered_backend_is_skipped_not_fatal():
    """A chain is a preference list. One unavailable entry should not refuse a run the rest can
    serve, so an unknown id drops out and the others still run."""
    reg = _registry("a")
    plan = Router(reg, RouterConfig(backends=("ghost", "a"))).route(_req())
    assert plan.eligible_ids == ["a"]


def test_no_request_shape_can_empty_a_chain_the_caller_declared():
    """The old router could return `chosen=None` for a document whose MIME type or requested
    feature every backend was judged unfit for, from data core could not verify. Nothing about
    the request can do that now: only the caller's own list decides."""
    reg = _registry("a", "b")
    req = _req(
        document={"bytes_base64": "eA==", "mime_type": "application/x-unheard-of"},
        features={"signatures": True, "handwriting": True},
    )
    plan = Router(reg, RouterConfig(backends=("a", "b"))).route(req)
    assert plan.eligible_ids == ["a", "b"]
