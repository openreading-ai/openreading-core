"""Cost accounting — the one place an adapter's `CostReport` becomes `response.usage`.

Adapters meter (`report_cost` projects `job.raw` counters through the pricing model); the router
accounts. That split is the response schema's own wording for `usage`: "the router computes
cost_usd via the pricing model, filling what the backend reports". Every path that turns a
finished Job into a response calls `apply_cost_report` right after `normalize()`.

Two rules keep the merge honest:

- **Fill-only.** Whatever `normalize()` already reported wins; the projection only fills slots the
  backend left unset. An adapter that meters a channel itself is never overwritten.
- **Never invent.** A quantity with no faithful `usage` slot is dropped rather than reshaped — a
  fractional page count is not rounded, and a combined token count is not split into
  input/output.
"""

from __future__ import annotations

from openreading.credentials import redact, secret_values
from openreading.types.cost import CostReport
from openreading.types.job import Job
from openreading.types.response import NormalizedResponse, Usage
from openreading.types.runtime import ResolvedCredentials


def merge_cost_report(resp: NormalizedResponse, report: CostReport) -> None:
    """Project `report` onto `resp.usage`, filling only fields the adapter left unset."""
    usage = resp.usage or Usage()
    if usage.cost_usd is None:
        usage.cost_usd = report.cost_usd
    if usage.cost_basis is None:
        usage.cost_basis = str(report.basis)
    if usage.duration_ms is None:
        usage.duration_ms = report.duration_ms

    qty = float(report.native_quantity)
    if report.native_unit == "page" and usage.pages_processed is None and qty.is_integer():
        usage.pages_processed = int(qty)
    elif report.native_unit == "credit" and usage.credits is None:
        usage.credits = qty
    resp.usage = usage


def apply_cost_report(
    adapter, job: Job, resp: NormalizedResponse, credentials: ResolvedCredentials | None = None
) -> NormalizedResponse:
    """Meter a finished job onto its response. `report_cost` is mandatory, but metering must never
    turn a successful run into a failure: an adapter whose meter raises degrades to the usage
    `normalize()` produced, plus a warning naming the backend.

    `credentials` (BL-93): the same `ResolvedCredentials` the caller already resolved for this
    adapter invocation (its `ctx.credentials` / equivalent) — optional so every pre-existing caller
    keeps working unchanged, but when passed, any secret value it holds is redacted (`***`) out of
    the warning message before it is attached. `report_cost` failures are caught HERE and never
    re-raised, so this is the one adapter-invocation boundary `auth_hinted` can never protect on its
    own — its `except AdapterError` only ever sees an exception that actually propagates."""
    try:
        merge_cost_report(resp, adapter.report_cost(job))
    except Exception as e:  # noqa: BLE001 - any meter failure degrades; the result still stands
        message = redact(
            f"report_cost failed: {type(e).__name__}: {e}",
            secret_values(adapter.descriptor, credentials),
        )
        resp.add_warning("cost_unavailable", message, adapter.descriptor.id)
    return resp
