"""Authorize general execution requests without reading inputs or running providers.

ExecutionConfig snapshots explicit configuration and independent backend and strategy scopes.
A backend is a registered document processor. A strategy is an operator-configured execution tree.
For example, allowing strategy main does not authorize its reducto leaf outside allowed_backends.
Strategy scope grants entrypoints, including their configured helpers, rather than individual use nodes.
An empty backend scope refuses every request. Policy ordering never grants backend access.

Configuration and requests are canonical bytes because frozen dataclasses do not freeze nested models.
Every returned model is rebuilt, so a caller cannot alter subsequent authorization through shared state.
No ambient YAML, environment variable, credential, source file or provider readiness is read here.
The request uses the existing request schema with acquisition and provisioning controls restricted.
Only a grant-relative source reference is accepted; credentials, runtime configuration and webhooks are operator responsibilities.
Document passwords and idempotency keys require separate retention contracts and are refused here.
The module does not filter formats. Each adapter decides whether it can process the supplied input.

ExecutionPlan lists possible scoped backends, not observed calls, price estimates or readiness promises.
A strategy's configured decider backend is included when authorized, even if its branch never runs.
The plan is internal preflight, not a durable capability, job receipt or exposed MCP tool.
Future workers must authorize again, acquire sources through Store.source, and copy from its open descriptor.
They must use explicit credentials, private journal/output directories and isolated stdout before provider dispatch.
The shared API must receive allowed_backends at execution time; preflight never replaces its per-dispatch checks.
General execution tools and their owned worker lifecycle remain unbuilt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jsonschema import ValidationError as SchemaError

from openreading.adapters.registry import BUILTIN_ADAPTERS, build_registry
from openreading.artifacts.intake import components
from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import json_bytes
from openreading.config import ConfigError, load, router_config
from openreading.router.router import Router
from openreading.schemas import validate_request
from openreading.strategies.loader import LoadedConfig, build_config, strip_strategy_prefix
from openreading.strategies.presets import PRESET_NAMES
from openreading.strategies.prune import compile_strategy
from openreading.types.errors import ScopeRefused
from openreading.types.request import OpenReadingRequest


class ExecutionRefused(Exception):
    """A fixed internal refusal code that excludes request values and configuration details."""

    def __init__(self, code: Literal["invalid_configuration", "invalid_request", "scope_denied"]):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExecutionConfig:
    """Operator authority independent of policy ordering and caller-controlled request values."""

    configuration: bytes
    allowed_backends: frozenset[str]
    allowed_strategies: frozenset[str]

    def __post_init__(self):
        try:
            if (
                type(self.configuration) is not bytes
                or type(self.allowed_backends) is not frozenset
                or type(self.allowed_strategies) is not frozenset
                or not self.allowed_backends <= BUILTIN_ADAPTERS.keys()
            ):
                raise ValueError
            configured = self.loaded.config.strategies.keys() | PRESET_NAMES
            if not self.allowed_strategies <= configured or "none" in self.allowed_strategies:
                raise ValueError
        except (ConfigError, ValueError, TypeError):
            raise ExecutionRefused("invalid_configuration") from None

    @classmethod
    def from_operator(
        cls,
        *,
        config: Path | dict | None = None,
        allowed_backends: Iterable[str] = (),
        allowed_strategies: Iterable[str] = (),
    ) -> ExecutionConfig:
        try:
            if isinstance(allowed_backends, str | bytes) or isinstance(
                allowed_strategies, str | bytes
            ):
                raise ValueError
            # load(None) discovers OPENREADING_CONFIG even with allow_cwd=False.
            loaded = load({"version": 1} if config is None else config, allow_cwd=False)
            assert loaded is not None
            return cls(
                json_bytes(loaded.raw), frozenset(allowed_backends), frozenset(allowed_strategies)
            )
        except (ConfigError, ValueError, TypeError):
            raise ExecutionRefused("invalid_configuration") from None

    @property
    def loaded(self) -> LoadedConfig:
        """Rebuild private mutable models from the configuration captured during setup."""
        raw = json.loads(self.configuration)
        if not isinstance(raw, dict):
            raise ValueError("Execution configuration must be a mapping.")
        loaded = build_config(load(raw, allow_cwd=False))
        assert loaded is not None
        return loaded

    @property
    def fingerprint(self) -> str:
        """Identify configuration and both scopes, independently of JSON key and set ordering."""
        return hashlib.sha256(
            json_bytes(
                {
                    "revision": "execution-authority.v1",
                    "configuration": json.loads(self.configuration),
                    "allowed_backends": sorted(self.allowed_backends),
                    "allowed_strategies": sorted(self.allowed_strategies),
                }
            )
        ).hexdigest()

    def authorize(self, value: dict) -> ExecutionPlan:
        """Resolve possible calls under this authority before acquiring any document or credential."""
        try:
            validate_request(value)
            req = OpenReadingRequest.model_validate(value)
            if (
                req.document.path is None
                or req.document.password is not None
                or req.backend.credentials_ref is not None
                or req.backend.runtime is not None
                or req.async_ is not None
                or req.idempotency_key is not None
            ):
                raise ValueError
            components(req.document.path)
        except (SchemaError, ValueError, TypeError, ArtifactError):
            raise ExecutionRefused("invalid_request") from None

        loaded = self.loaded
        strategy = strip_strategy_prefix(req.backend.id)
        if req.backend.id is None and loaded.config.defaults:
            strategy = loaded.config.defaults.strategy or None
        if req.backend.id == "strategy:none":
            strategy = None
        elif strategy is not None and strategy not in self.allowed_strategies:
            raise ExecutionRefused("scope_denied")
        if (
            req.backend.id is not None
            and not req.backend.id.startswith("strategy:")
            and req.backend.id not in self.allowed_backends
        ):
            raise ExecutionRefused("scope_denied")

        registry = build_registry()
        routing = router_config(loaded.config.policy)
        try:
            if strategy is None:
                scoped = Router(registry, routing).route(req).restrict_to(self.allowed_backends)
                backends = tuple(scoped.eligible_ids)
            else:
                compiled = compile_strategy(
                    req,
                    strategy,
                    loaded.config,
                    registry,
                    routing,
                    plain_info=loaded.plain_info,
                    backend_allowlist=self.allowed_backends,
                )
                possible = list(compiled.dispatchable)
                if compiled.decider and compiled.decider.backend in self.allowed_backends:
                    possible.append(compiled.decider.backend)
                backends = tuple(dict.fromkeys(possible))
        except ScopeRefused:
            raise ExecutionRefused("scope_denied") from None
        except (ValueError, KeyError):
            raise ExecutionRefused("invalid_configuration") from None
        if not backends:
            raise ExecutionRefused("scope_denied")
        return ExecutionPlan(self, json_bytes(req.to_schema_dict()), backends, strategy)


@dataclass(frozen=True)
class ExecutionPlan:
    """A preflight snapshot with no acquired source, resolved credential or execution side effect."""

    authority: ExecutionConfig
    request_json: bytes
    backends: tuple[str, ...]
    strategy: str | None

    @property
    def request(self) -> OpenReadingRequest:
        """Return a fresh request model so downstream edits cannot mutate this snapshot."""
        return OpenReadingRequest.model_validate_json(self.request_json)
