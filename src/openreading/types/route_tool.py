"""Describe an authorized route without claiming extraction or measured readiness.

The route-tool.v0.1 receipt names the surviving backend chain and excluded entries.
An empty chain carries a terminal reason. For example, an excluded named backend
returns scope_denied without constructing an adapter or resolving its credentials.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class RouteReceipt(BaseModel):
    """A metadata-only plan that has neither read a source nor executed a backend."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["0.1"] = "0.1"
    execution: Literal["not_started"] = "not_started"
    allowed_backends: list[str]
    chain: list[str]
    dropped: list[str]
    terminal_reason: Literal["scope_denied", "no_backend_in_scope"] | None = None

    def wire(self) -> dict:
        return self.model_dump(mode="json")
