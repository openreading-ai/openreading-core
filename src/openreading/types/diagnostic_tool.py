"""Describe authorized backend discovery and explicit diagnostics without document acquisition.

Static discovery reports descriptor claims, while readiness reports local configuration rather than provider acceptance.
For example, a resolved API key can satisfy readiness while a later liveness probe reports unauthorized.
The diagnostic worker receives only the operator's forwarded environment, matching general execution setup.
Liveness preserves the shared report and never accepts endpoint overrides, documents or caller credentials.
"""

from copy import deepcopy
from typing import Literal

from pydantic import Field, RootModel

from openreading.artifacts.models import ErrorEnvelope
from openreading.schemas import liveness_report_schema
from openreading.types.descriptor import AdapterDescriptor
from openreading.types.execution_job import JobWire
from openreading.types.execution_tool import ExecutionToolFailure
from openreading.types.liveness import LivenessReport


class CatalogRequest(JobWire):
    backend: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class ReadinessRequest(JobWire):
    backend: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class LivenessRequest(ReadinessRequest):
    timeout_s: float = Field(default=5.0, ge=0.1, le=30, allow_inf_nan=False)


class BackendCatalog(JobWire):
    schema_version: Literal["0.1"] = "0.1"
    scope: Literal["authorized_general_backends"] = "authorized_general_backends"
    readiness: Literal["not_checked"] = "not_checked"
    backends: list[AdapterDescriptor]


class Readiness(JobWire):
    slug: str
    type: str
    extra_installed: bool
    missing_deps: list[str]
    creds_found: list[str]
    creds_missing: list[str]
    required_missing: list[str]
    signup_url: str | None
    ready: bool


class ReadinessReport(JobWire):
    schema_version: Literal["0.1"] = "0.1"
    scope: Literal["explicit_execution_environment"] = "explicit_execution_environment"
    readiness: Readiness


def diagnostic_tool_contract() -> dict:
    """Generate a standalone contract without changing existing descriptor or liveness schemas."""
    model = RootModel[
        CatalogRequest
        | ReadinessRequest
        | LivenessRequest
        | BackendCatalog
        | ReadinessReport
        | LivenessReport
        | ExecutionToolFailure
        | ErrorEnvelope
    ]
    schema = model.model_json_schema()
    liveness = deepcopy(liveness_report_schema())
    for key in ("$schema", "$id"):
        liveness.pop(key, None)
    schema["$defs"]["LivenessReport"] = liveness
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://openreading.ai/schemas/diagnostic-tool.v0.1.json",
            "title": "OpenReading Diagnostic Tool v0.1",
        }
    )
    return schema
