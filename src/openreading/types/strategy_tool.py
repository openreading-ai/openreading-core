"""Describe authorized strategy inspection without accepting configuration or source acquisition.

The tree view omits free-text and option payloads instead of presenting them as execution configuration.
For example, a backend remains named while its caller-specific options disappear with an explicit omission count.
Validation covers the selected entrypoint and referenced helpers, not unrelated operator strategies or runtime readiness.
"""

from typing import Any, Literal

from pydantic import Field, RootModel, model_validator

from openreading.artifacts.models import ErrorEnvelope
from openreading.schemas import execution_tool_schema
from openreading.types.execution_job import JobWire
from openreading.types.execution_tool import ExecutionToolFailure


class StrategyRequest(JobWire):
    operation: Literal["list", "show", "normalize", "validate", "plan"]
    strategy: str | None = Field(default=None, min_length=1, max_length=256)
    request: dict[str, Any] | None = None

    @model_validator(mode="after")
    def operation_fields(self):
        if (self.operation == "list") != (self.strategy is None):
            raise ValueError("Only list omits a strategy.")
        if self.request is not None and self.operation != "plan":
            raise ValueError("Only plan accepts request metadata.")
        return self


class StrategyList(JobWire):
    schema_version: Literal["0.1"] = "0.1"
    strategies: list[str]


class StrategyValidation(JobWire):
    schema_version: Literal["0.1"] = "0.1"
    strategy: str
    scope: Literal["entrypoint_and_helpers"] = "entrypoint_and_helpers"
    valid: bool
    error_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    diagnostics: Literal["details_omitted"] = "details_omitted"
    readiness: Literal["not_checked"] = "not_checked"


class StrategyView(JobWire):
    schema_version: Literal["0.1"] = "0.1"
    strategy: str
    scope: Literal["authorized_strategy_view"] = "authorized_strategy_view"
    configuration_verified: Literal[False] = False
    tree: dict[str, Any]
    trees: dict[str, dict[str, Any]]
    dispatchable: list[str]
    omitted: int = Field(ge=0)
    interpretation: Literal["structural_view_not_executable_configuration"] = (
        "structural_view_not_executable_configuration"
    )


def strategy_tool_contract() -> dict:
    """Bind plan metadata to the same restricted request contract as general execution."""
    schema = RootModel[
        StrategyRequest
        | StrategyList
        | StrategyValidation
        | StrategyView
        | ExecutionToolFailure
        | ErrorEnvelope
    ].model_json_schema()
    schema["$defs"]["StrategyRequest"]["allOf"] = [
        {
            "if": {"properties": {"operation": {"const": "list"}}},
            "then": {"properties": {"strategy": {"type": "null"}}},
            "else": {"required": ["strategy"], "properties": {"strategy": {"type": "string"}}},
        },
        {
            "if": {
                "properties": {"operation": {"enum": ["list", "show", "normalize", "validate"]}}
            },
            "then": {"properties": {"request": {"type": "null"}}},
        },
    ]
    schema["$defs"]["ParseRequest"] = execution_tool_schema()["$defs"]["ParseRequest"]
    schema["$defs"]["StrategyRequest"]["properties"]["request"] = {
        "anyOf": [{"$ref": "#/$defs/ParseRequest"}, {"type": "null"}],
        "default": None,
    }
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://openreading.ai/schemas/strategy-tool.v0.1.json",
            "title": "OpenReading Strategy Tool v0.1",
        }
    )
    return schema
