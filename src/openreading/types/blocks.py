"""Block / table / field / chunk models — the response's structural payload.

Pydantic constructors that serialize (via `mode="json", exclude_none=True`) into the
response JSON Schema. `native_type` always travels alongside the normalized `type` so a
lossy down-map to `OTHER` never loses the backend's own label.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openreading.types.enums import BlockType, TextType
from openreading.types.geometry import BBox


class TableCell(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row: int | None = None
    col: int | None = None
    row_span: int = 1
    col_span: int = 1
    text: str | None = None
    is_header: bool | None = None
    bbox: BBox | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)  # C7: [0,1]


class Table(BaseModel):
    """Populated only when a Block's type is TABLE. Carries BOTH a cell grid and a
    list-of-lists so no single table representation is forced (normalized_schema.md §6.6)."""

    model_config = ConfigDict(extra="forbid")

    n_rows: int | None = None
    n_cols: int | None = None
    cells: list[TableCell] | None = None
    rows: list[list[str | None]] | None = None


class Block(BaseModel):
    """One reading-order element (response schema $defs.Block)."""

    model_config = ConfigDict(extra="forbid")

    type: BlockType
    id: str | None = None
    native_type: str | None = None
    text: str | None = None
    markdown: str | None = None
    html: str | None = None
    bbox: BBox | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reading_order: int | None = None
    table: Table | None = None
    children: list[str] | None = None
    text_type: TextType | None = None


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int | None = None
    bbox: BBox | None = None
    text: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)  # C7: [0,1]


class TypedField(BaseModel):
    """One entry in the response `typed_fields` map. `confidence` may be numeric [0,1] or a
    backend's qualitative enum preserved as a string (normalized_schema.md §6)."""

    model_config = ConfigDict(extra="forbid")

    value: Any = None
    type: str | None = None
    normalized_value: Any = None
    confidence: float | str | None = None
    citations: list[Citation] | None = None


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    text: str | None = None
    markdown: str | None = None
    block_ids: list[str] | None = None
    page_span: list[int] | None = None
    embedding: list[float] | None = None
