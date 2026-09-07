"""CostReport, the consumption record every adapter returns.

`native_unit` mirrors the unit a backend meters in, so `report_cost()` is a projection of what
the vendor already said rather than a conversion. Pages, credits, tokens and seconds are
observations: they arrive in a response body or come off a clock on this machine.

Dollars are not, and this module used to carry them. Each hosted adapter kept a private price
table, `report_cost` multiplied the vendor's counter by it, and the product reached the caller as
`usage.cost_usd` beside counters that were measured. Nothing in the package could tell the two
apart, and nothing could notice when a vendor repriced. Core holds no fact it cannot verify, so
the tables, `cost_usd`, `cost_basis` and `billing_target` are gone. A caller who wants money
multiplies these counters by the prices on their own invoice, which is the only place their tier
and their negotiated rate exist.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostReport:
    """What a backend consumed, in the units it reported.

    Counters only. `cost_usd`, `basis` and `billing_target` are gone: converting a count into a
    price needed a per-vendor rate this package kept in its own source and could not verify, and
    the derived figure landed on the response beside counters that were genuinely measured. A
    caller who wants dollars multiplies these by the prices on their own invoice, which carries
    their tier and their negotiated rate.
    """

    native_unit: str  # "page" | "credit" | "token" | "doc" | "gpu_second" | "cpu_second"
    native_quantity: float
    breakdown: list[dict] | None = None
    duration_ms: int | None = None


def infra_only(
    native_unit: str, native_quantity: float, duration_ms: int | None = None
) -> CostReport:
    """Convenience for local backends: the work they did, in their own unit.

    The name survives the price removal because it still says the useful thing. A local backend
    consumes the caller's own machine, so the only figures it can report are a count and a
    duration.
    """
    return CostReport(
        native_unit=native_unit,
        native_quantity=native_quantity,
        duration_ms=duration_ms,
    )
