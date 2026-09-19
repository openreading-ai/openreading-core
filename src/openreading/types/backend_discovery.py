"""Describe the configured local backend without claiming measured readiness.

The backend-discovery.v0.1 schema wraps unchanged adapter descriptors and the
operator's OCR setting. Descriptor claims do not establish configured output.
For example, a declared OCR capability does not mean this profile enabled OCR.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from openreading.types.descriptor import AdapterDescriptor


class BackendDiscovery(BaseModel):
    """One fixed profile's descriptor, with no credentials or local asset paths."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["0.1"] = "0.1"
    scope: Literal["configured_local_profile"] = "configured_local_profile"
    readiness: Literal["not_checked"] = "not_checked"
    ocr_enabled: bool
    backends: list[AdapterDescriptor] = Field(min_length=1, max_length=1)

    def wire(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)
