"""Preserve provider text and blocks using the existing synthetic-container contract.

Docling's sheet and slide coordinates are not PDF physical-page coordinates.
The normalized container carries page_attribution_unavailable, no dimensions, and no boxes.
Its block IDs, labels, children and table cells come directly from provider items.
The artifact layer cites JSON locations rather than the synthetic container's number.
"""

from openreading.derive import GridCell, cells_to_grid, table_to_text
from openreading.types.blocks import Block
from openreading.types.enums import BackendType, BlockType, ResponseState
from openreading.types.request import Outputs
from openreading.types.response import BackendInfo, Document, NormalizedResponse, Page, Status


def project_unpaginated(payload: dict, outputs: Outputs) -> NormalizedResponse:
    blocks = []
    for index, item in enumerate(payload["items"]):
        label = item.get("label", "text")
        text = item.get("orig") if label == "list_item" else item.get("text")
        if not isinstance(text, str) or not text:
            text = item.get("text")
        table = None
        kind = BlockType.TITLE if label in {"title", "section_header"} else BlockType.TEXT
        if label == "table":
            kind = BlockType.TABLE
            cells = []
            for cell in item.get("data", {}).get("table_cells", []):
                row, col = cell.get("start_row_offset_idx", 0), cell.get("start_col_offset_idx", 0)
                cells.append(
                    GridCell(
                        row=row,
                        col=col,
                        row_span=max(1, cell.get("end_row_offset_idx", row + 1) - row),
                        col_span=max(1, cell.get("end_col_offset_idx", col + 1) - col),
                        text=cell.get("text") or None,
                        is_header=bool(cell.get("column_header") or cell.get("row_header")),
                    )
                )
            table = cells_to_grid(cells)
            text = table_to_text(table) or None
        elif label == "picture":
            kind = BlockType.FIGURE
        blocks.append(
            Block(
                id=item.get("self_ref", f"d{index}"),
                type=kind,
                native_type=label,
                text=text,
                reading_order=index,
                children=[child["cref"] for child in item.get("children", [])] or None,
                table=table if outputs.tables == "cells" else None,
            )
        )
    pages = None
    if blocks and outputs.blocks:
        pages = [
            Page(
                page_number=1,
                blocks=blocks,
                text="\n".join(block.text for block in blocks if block.text)
                if outputs.text
                else None,
            )
        ]
    response = NormalizedResponse(
        status=Status(
            state=ResponseState.PARTIAL if payload.get("partial") else ResponseState.SUCCEEDED
        ),
        backend=BackendInfo(id="docling_local", type=BackendType.OSS_LIBRARY),
        document=Document(text=payload["document_text"] if outputs.text else None, pages=pages),
        channel_provenance={"text": "native", "blocks": "native"},
    )
    response.add_warning(
        "page_attribution_unavailable",
        "The block container does not identify a physical page. Cite normalized text locations.",
        "blocks",
    )
    if outputs.tables == "cells" and any(block.table is not None for block in blocks):
        response.channel_provenance = {
            **(response.channel_provenance or {}),
            "table_cells": "native",
        }
    if payload.get("omitted_furniture_items"):
        response.add_warning(
            "furniture_text_omitted",
            "Headers, footers, and other furniture text are excluded from block evidence.",
            "text",
        )
    if payload.get("partial"):
        response.add_warning(
            "partial_conversion", "Local Docling returned a partial extraction.", "text"
        )
    for channel, requested in [
        ("markdown", outputs.markdown),
        ("typed_fields", outputs.typed_fields),
    ]:
        if requested:
            response.add_warning(
                "unsupported_channel", f"This local conversion does not provide {channel}.", channel
            )
    return response
