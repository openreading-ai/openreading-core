"""Construct versioned selection receipts without exposing the user's original path.

A successful selection names a copied input, not a parsed artifact. The import tool
creates evidence later. Failures use fixed text and never include provider exceptions.
The v0.2 schema preserves v0.1 single-file receipts and adds paginated batch receipts.
A trusted provider may yield SelectionBatch or its references/skipped mapping. References
are copied inputs, never model-selected paths. Skipped counts describe encountered entries,
not files inside skipped directories. They do not assert complete folder coverage.
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SelectionCode = Literal[
    "selection_unavailable", "selection_cancelled", "selection_timeout", "selection_failed", "busy"
]
MESSAGES: dict[SelectionCode, str] = {
    "selection_unavailable": "This server has no local document selection provider.",
    "selection_cancelled": "Document selection was cancelled. No reference was returned.",
    "selection_timeout": "Document selection exceeded its deadline. Start a new selection when ready.",
    "selection_failed": "Local selection or receipt retrieval failed. No new references were returned.",
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


# Trusted providers own copies until their context exits normally. This is not a tool argument.
@dataclass(frozen=True)
class SelectionBatch:
    references: tuple[str, ...]
    skipped: dict[str, int]


class SelectionPage(SelectionModel):
    """A bounded page of copied references, never a live directory grant."""

    schema_version: Literal["0.2"] = "0.2"
    selection_id: str = Field(pattern=r"^s1_[0-9a-f]{32}$")
    items: list[SelectionReceipt] = Field(max_length=16)
    total_files: int = Field(ge=0)
    total_bytes: int = Field(ge=0)
    skipped: dict[
        Literal["hidden", "package", "symlink", "unsupported", "duplicate", "special"],
        Annotated[int, Field(ge=0)],
    ]
    next_cursor: str | None = Field(default=None, max_length=512)
