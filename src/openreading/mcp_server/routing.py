"""Plan backend order through the shared router under an operator-owned scope.

RoutingConfig captures explicit setup once. It never discovers ambient YAML files.
The policy supplies ordering, while allowed_backends independently limits access.
For example, a policy naming reducto cannot authorize reducto outside that set.
Default setup restricts planning to the selected local import backend.

Planning constructs no execution context and performs no document or credential reads.
The router does not inspect document content, so this tool accepts no source argument.
Its plan establishes neither backend readiness nor document compatibility.
Strategy planning remains separate because a strategy has branches rather than one chain.
Changing this setup does not change the existing local import engine or artifact identity.
"""

from dataclasses import dataclass
from pathlib import Path

from openreading.adapters.registry import BUILTIN_ADAPTERS, build_registry
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import json_bytes
from openreading.config import load, router_config
from openreading.router.compliance import RouterConfig
from openreading.router.router import Router
from openreading.types.request import OpenReadingRequest
from openreading.types.route_tool import RouteReceipt

MAX_ROUTE_BYTES = 65536


@dataclass(frozen=True)
class RoutingConfig:
    """An immutable snapshot of operator ordering and independent backend authorization."""

    default_backends: tuple[str, ...]
    allowed_backends: frozenset[str]

    def __post_init__(self):
        if (
            type(self.default_backends) is not tuple
            or any(not isinstance(item, str) for item in self.default_backends)
            or type(self.allowed_backends) is not frozenset
            or any(item not in BUILTIN_ADAPTERS for item in self.allowed_backends)
        ):
            raise ValueError(
                "Routing setup requires registered backend identifiers and immutable sets."
            )

    @classmethod
    def from_operator(
        cls,
        default_backend: str,
        *,
        config: Path | dict | None = None,
        allowed_backends: list[str] | None = None,
    ) -> "RoutingConfig":
        # Passing None to load discovers environment configuration even with allow_cwd=False.
        # Absence here means no discovery, so an unrelated host environment cannot widen routing.
        loaded = load(config, allow_cwd=False) if config is not None else None
        defaults = router_config(loaded.policy if loaded else None).backends
        return cls(
            default_backends=tuple(defaults) if defaults is not None else (default_backend,),
            allowed_backends=frozenset(
                [default_backend] if allowed_backends is None else allowed_backends
            ),
        )


def plan_route(
    config: RoutingConfig, *, backend: str | None = None, fallback: list[str] | None = None
) -> RouteReceipt:
    """Reuse router ordering and scope pruning without executing the returned chain."""
    if backend is not None and backend not in config.allowed_backends:
        result = RouteReceipt(
            allowed_backends=sorted(config.allowed_backends),
            chain=[],
            dropped=[backend],
            terminal_reason="scope_denied",
        )
    else:
        # The router reads backend and routing fields only. No path is materialized here.
        request = OpenReadingRequest.model_validate(
            {
                "document": {"bytes_base64": ""},
                "backend": {"id": backend},
                "routing": {"fallback": fallback},
            }
        )
        plan = Router(build_registry(), RouterConfig(backends=config.default_backends)).route(
            request
        )
        scoped = plan.restrict_to(config.allowed_backends)
        result = RouteReceipt(
            allowed_backends=sorted(config.allowed_backends),
            chain=scoped.eligible_ids,
            dropped=list(scoped.dropped),
            terminal_reason=(
                None if scoped.chain else "scope_denied" if plan.chain else "no_backend_in_scope"
            ),
        )
    if len(json_bytes(result.wire())) > MAX_ROUTE_BYTES:
        raise ArtifactError("response_too_large")
    return result
