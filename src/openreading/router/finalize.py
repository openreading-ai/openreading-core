"""The one path from an adapter's output to a response, and the one path a request takes in.

You get a response that carries `usage` and, when the request asked for extraction with a JSON
Schema, a `schema_check` (response v0.4). Every execution surface calls these helpers instead of
the adapter's own `submit` and `normalize`: `openreading.api`, `openreading.router.executor`,
`openreading.strategies.engine`, `openreading.strategies.calibrate`, `openreading.server.app`,
`openreading.evals.runner` and the conformance kit in `openreading.testing.conformance`.

- `submit(adapter, req, ctx)` and `submit_many(adapter, reqs, ctx)` hand the adapter a copy of
  each request whose `extraction_schema.json_schema` has every `x-read-*` keyword removed
  (`openreading.derive.fields.strip_annotations`). The caller keeps the labelled original for
  the check and the journal. Failure it avoids: a strict structured-output provider refusing the
  request over a keyword meant only for core.
- `finalize_response(adapter, job, ctx, req)` calls `normalize` with the stripped slim request,
  then `apply_cost_report` (`openreading.router.cost`), then `apply_schema_check` against the
  labelled `req`.
- `apply_schema_check(resp, req)` measures `resp.typed_fields` against the request's schema,
  fills `TypedField.type` where the adapter left it unset, and adds the warnings
  `critical_field_missing`, `critical_field_invalid` and `intent_all_critical`. It first removes
  any `schema_check` and any of those warnings already present, so a second call on the same
  response is safe. The executor calls it on a cache hit, and the strategy engine calls it on a
  merged result, because each can change what the check sees.

When the check runs: a response carries `schema_check` whenever the request carries
`extraction_schema.json_schema` and a response comes back, `succeeded` or `partial`. With no
`typed_fields` every field is `missing`. A request with `extraction_schema` but no `json_schema`
gets no check, and a request without `extraction_schema` gets nothing new at all.

The check reports and never acts. It changes no status, triggers no retry or fallback, and edits
no value. It reads the request and the response, never the backend type, so the router gains no
branch per backend. Failure it avoids: a per-adapter check written ten times and drifting ten
ways. A test fails on any direct `adapter.normalize(` call outside this module, the adapters
themselves and the conformance kit.
"""

from __future__ import annotations

from openreading import schema_bank
from openreading.derive.fields import (
    CHECK_WARNING_CODES,
    check_typed_fields,
    check_warnings,
    fill_field_types,
    strip_annotations,
)
from openreading.ledger.header import slim_request
from openreading.router.cost import apply_cost_report
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest
from openreading.types.response import NormalizedResponse
from openreading.types.runtime import RunContext


def strip_request(req: OpenReadingRequest) -> OpenReadingRequest:
    """`req` with `x-read-*` removed from its extraction schema. Never mutates `req`."""
    es = req.extraction_schema
    if es is None or es.json_schema is None:
        return req
    stripped = es.model_copy(update={"json_schema": strip_annotations(es.json_schema)})
    return req.model_copy(update={"extraction_schema": stripped})


def submit(adapter, req: OpenReadingRequest, ctx: RunContext) -> Job:
    """`adapter.submit` with the stripped request."""
    return adapter.submit(strip_request(req), ctx)


def submit_many(adapter, reqs: list[OpenReadingRequest], ctx: RunContext) -> Job:
    """`adapter.submit_many` with every request stripped."""
    return adapter.submit_many([strip_request(r) for r in reqs], ctx)


def normalize(adapter, job: Job, ctx: RunContext, req: OpenReadingRequest) -> NormalizedResponse:
    """`adapter.normalize` with the stripped slim request, and nothing else applied."""
    return adapter.normalize(job, ctx, slim_request(strip_request(req)))


def finalize_response(
    adapter, job: Job, ctx: RunContext, req: OpenReadingRequest
) -> NormalizedResponse:
    """Normalize, meter and check one finished job. `req` is the labelled request."""
    resp = normalize(adapter, job, ctx, req)
    apply_cost_report(adapter, job, resp, ctx.credentials)
    return apply_schema_check(resp, req)


def apply_schema_check(resp: NormalizedResponse, req: OpenReadingRequest) -> NormalizedResponse:
    """Replace `resp.schema_check` and its warnings with a check against `req`. Idempotent."""
    if resp.warnings:
        resp.warnings = [w for w in resp.warnings if w.code not in CHECK_WARNING_CODES] or None
    resp.schema_check = None
    es = req.extraction_schema
    schema = es.json_schema if es is not None else None
    if schema is None:
        return resp
    # A request that asked for fields and got none returns an empty map, never an absent one.
    # The empty map is the honest answer, and the check below reports every field missing.
    # Without it, a reply with no fields and no text breaks the response contract, which
    # requires one channel, and the CLI and server refuse to emit it.
    if resp.typed_fields is None:
        resp.typed_fields = {}
    fill_field_types(schema, resp.typed_fields)
    check = check_typed_fields(schema, resp.typed_fields)
    # A hash match is a fact core can verify. A request field naming the entry would be a claim.
    check.schema_ref = schema_bank.ref_for_hash(check.schema_hash)
    resp.schema_check = check
    for code, message, field in check_warnings(schema, check):
        resp.add_warning(code, message, field)
    return resp
