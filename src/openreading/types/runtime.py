"""RunContext / Health / RawResult (adapter_interface.md §1.5, §1.2).

RunContext carries everything an adapter needs that is NOT in the document request — most
importantly resolved credentials (from request.backend.credentials_ref via the secret
broker; NEVER the raw secret in the request body).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResolvedCredentials:
    """Opaque bag the secret broker fills from credentials_ref. Adapter-specific keys."""

    values: dict[str, Any] = field(default_factory=dict)
    source: str | None = None  # e.g. "env", "vault:...", "aws-role:arn:..."

    def __repr__(self) -> str:
        # NEVER print credential VALUES (a stray log/repr must not leak a secret). Keys only.
        return f"ResolvedCredentials(keys={sorted(self.values)!r}, source={self.source!r})"


@dataclass
class RunContext:
    credentials: ResolvedCredentials | None = None
    compliance: dict | None = None  # request.compliance, already checked; for adapter self-checks
    idempotency_key: str | None = None
    runtime: dict | None = (
        None  # request.backend.runtime (mode/image/endpoint/device/system_deps_ok)
    )
    deadline_ms: int | None = None
    workdir: str | None = None  # sandboxed temp dir for subprocess/upload spooling


@dataclass
class Health:
    ready: bool
    detail: str = ""
    version: str | None = None
    cold: bool = False
    missing_deps: list[str] = field(default_factory=list)


@dataclass
class RawResult:
    """The untouched backend output carried on the Job so normalize() and report_cost()
    both read it: decoded JSON for hosted APIs, or the native object / serialized projection
    for libraries and models (normalized_schema.md §5)."""

    payload: Any
    media_type: str | None = None
    object_class: str | None = None
    encoding: str | None = None  # RawEncoding value; how payload should be embedded in backend_raw
