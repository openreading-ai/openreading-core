"""CostReport, the one cost record every adapter returns.

`native_unit` mirrors the unit a backend actually prices in, so `report_cost()` is a thin
projection rather than a conversion. `billing_target` carries the pass-through fact: BYO-key
means the charge lands on the caller's own account ("caller_account"), local backends bill the
caller's own infrastructure ("caller_infra"), and "openreading" (resale) is deliberately never
used, because every charge is a pass-through to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass

from openreading.types.enums import CostBasis


@dataclass
class CostReport:
    native_unit: str  # "page" | "credit" | "token" | "doc" | "gpu_second" | "cpu_second"
    native_quantity: float
    cost_usd: float | None  # None for INFRA_ONLY unless an infra model is applied
    basis: CostBasis
    billing_target: str  # "caller_account" | "caller_infra"  (never "openreading")
    breakdown: list[dict] | None = None
    duration_ms: int | None = None
    _valid_targets = ("caller_account", "caller_infra")

    def __post_init__(self) -> None:
        if self.billing_target not in self._valid_targets:
            raise ValueError(
                f"billing_target must be one of {self._valid_targets} "
                f"(pure pass-through: 'openreading' resale is disallowed); got "
                f"{self.billing_target!r}"
            )


def infra_only(
    native_unit: str, native_quantity: float, duration_ms: int | None = None
) -> CostReport:
    """Convenience for local backends: compute cost, no per-call price, billed to caller infra."""
    return CostReport(
        native_unit=native_unit,
        native_quantity=native_quantity,
        cost_usd=None,
        basis=CostBasis.INFRA_ONLY,
        billing_target="caller_infra",
        duration_ms=duration_ms,
    )
