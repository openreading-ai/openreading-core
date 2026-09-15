"""Construct selection-tool v0.1 receipts without exposing the user's original path.

A successful selection names a copied input, not a parsed artifact. The import tool
creates evidence later. Failures use fixed text and never include provider exceptions.
These models mirror selection-tool.v0.1.json independently of extraction payload versions.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SelectionCode = Literal[
    "selection_unavailable", "selection_cancelled", "selection_timeout", "selection_failed", "busy"
]
MESSAGES: dict[SelectionCode, str] = {
    "selection_unavailable": "This server has no local document selection provider.",
    "selection_cancelled": "Document selection was cancelled. No reference was returned.",
    "selection_timeout": "Document selection exceeded its deadline. Start a new selection when ready.",
    "selection_failed": "Cannot select this document. Choose a readable file within the local intake limits.",
    "busy": "Another document selection is pending. Finish or cancel that selection first.",
}


class SelectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    def wire(self) -> dict:
        return self.model_dump(mode="json")


class SelectionReceipt(SelectionModel):
    schema_version: Literal["0.1"] = "0.1"
    path: str = Field(min_length=1, max_length=1024)
    display_name: str = Field(min_length=1, max_length=1024)
    source_bytes: int = Field(gt=0)


class SelectionError(SelectionModel):
    code: SelectionCode
    message: str
    retryable: bool


class SelectionFailure(SelectionModel):
    schema_version: Literal["0.1"] = "0.1"
    error: SelectionError

    @classmethod
    def from_code(cls, code: SelectionCode) -> "SelectionFailure":
        return cls(
            error=SelectionError(code=code, message=MESSAGES[code], retryable=code == "busy")
        )
