"""Project Docling item spans into physical pages without guessing cross-page text.

Every provenance range must fit the item's text and name a known physical page.
Overlapping ranges are ambiguous and omit the item with a warning. Geometry remains
optional. Page origin summarizes the measured native/OCR cells conservatively.
Furniture text, such as a running header, stays out of page evidence with a warning,
because a search that cannot see it must not look like proof the words are absent.
"""

from __future__ import annotations

import math
from typing import Literal, cast

from openreading.types.blocks import Block
from openreading.types.enums import (
    BackendType,
    BlockType,
    NativeOrigin,
    NativeUnit,
    PageUnit,
    ResponseState,
)
from openreading.types.geometry import to_canonical
from openreading.types.request import Outputs
from openreading.types.response import BackendInfo, Document, NormalizedResponse, Page, Status

TextOrigin = Literal["native", "ocr", "mixed"]


def _geometry(prov: dict, size: dict, page: int):
    box = prov.get("bbox")
    if not isinstance(box, dict) or box.get("coord_origin") not in {"TOPLEFT", "BOTTOMLEFT"}:
        return None
    try:
        coords = [box[key] for key in ("l", "t", "r", "b")]
        if not all(math.isfinite(float(v)) for v in [*coords, size["width"], size["height"]]):
            return None
        return to_canonical(
            coords,
            origin=NativeOrigin.BOTTOM_LEFT
            if box["coord_origin"] == "BOTTOMLEFT"
            else NativeOrigin.TOP_LEFT,
            unit=NativeUnit.PDF_POINT,
            page_width=size["width"],
            page_height=size["height"],
            page=page,
        )
    except (KeyError, TypeError, ValueError):
        return None


def project_document(
    payload: dict, outputs: Outputs
) -> tuple[NormalizedResponse, dict[int, TextOrigin]]:
    metadata = payload["pages"]
    numbers = sorted(int(n) for n in metadata)
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError("Local PDF page metadata is incomplete.")
    blocks: dict[int, list[Block]] = {n: [] for n in numbers}
    warning_codes = set()
    for index, item in enumerate(payload["items"]):
        text = item.get("text")
        if not isinstance(text, str) or not text:
            if item.get("label") == "table":
                warning_codes.add("table_text_unavailable")
            continue
        spans = []
        try:
            for prov in item.get("prov", []):
                page = prov["page_no"]
                start, end = prov["charspan"]
                if (
                    type(page) is not int
                    or page not in blocks
                    or type(start) is not int
                    or type(end) is not int
                    or not 0 <= start < end <= len(text)
                ):
                    raise ValueError
                spans.append((start, end, page, prov))
            spans.sort(key=lambda span: span[:3])
            if not spans or any(a[1] > b[0] for a, b in zip(spans, spans[1:], strict=False)):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            warning_codes.add("ambiguous_page_provenance")
            continue
        if (
            spans[0][0] != 0
            or spans[-1][1] != len(text)
            or any(a[1] < b[0] for a, b in zip(spans, spans[1:], strict=False))
        ):
            warning_codes.add("ambiguous_page_provenance")
        for start, end, page, prov in spans:
            label = item.get("label", "text")
            kind = BlockType.TITLE if label in {"title", "section_header"} else BlockType.TEXT
            blocks[page].append(
                Block(
                    type=kind,
                    id=f"d{index}-p{page}-s{start}",
                    native_type=label,
                    text=text[start:end],
                    reading_order=len(blocks[page]),
                    bbox=_geometry(prov, metadata[str(page)].get("size", {}), page),
                )
            )
    if payload.get("omitted_furniture_items"):
        warning_codes.add("furniture_text_omitted")
    pages = []
    origins: dict[int, TextOrigin] = {}
    for number in numbers:
        size = metadata[str(number)].get("size", {})
        text = "\n".join(b.text or "" for b in blocks[number])
        if not text.strip():
            warning_codes.add("unreadable_pages")
        origin = payload.get("page_origins", {}).get(str(number), "mixed")
        if origin not in {"native", "ocr", "mixed"}:
            origin = "mixed"
        origins[number] = cast(TextOrigin, origin)
        pages.append(
            Page(
                page_number=number,
                width=size.get("width"),
                height=size.get("height"),
                unit=PageUnit.PDF_POINT,
                text=text if outputs.text else None,
                blocks=blocks[number] if outputs.blocks else None,
            )
        )
    text = "\n\n".join("\n".join(b.text or "" for b in blocks[n]) for n in numbers if blocks[n])
    response = NormalizedResponse(
        status=Status(
            state=ResponseState.PARTIAL if payload.get("partial") else ResponseState.SUCCEEDED
        ),
        backend=BackendInfo(id="docling_local", type=BackendType.OSS_LIBRARY),
        document=Document(page_count=len(pages), pages=pages, text=text if outputs.text else None),
    )
    if payload.get("partial"):
        response.add_warning(
            "partial_conversion", "Local Docling returned a partial extraction.", "text"
        )
    messages = {
        "table_text_unavailable": "A table region has no independently available text; table structure is disabled.",
        "ambiguous_page_provenance": "Text without unambiguous physical-page spans was omitted from page evidence.",
        "unreadable_pages": "Some physical pages have no page-addressable text; check the source and OCR setting.",
        "furniture_text_omitted": "Page headers, footers, and other furniture text are excluded from page evidence.",
    }
    for code in sorted(warning_codes):
        response.add_warning(code, messages[code], "text")
    for channel, requested in [
        ("markdown", outputs.markdown),
        ("typed_fields", outputs.typed_fields),
        ("table_cells", outputs.tables == "cells"),
    ]:
        if requested:
            response.add_warning(
                "unsupported_channel", f"This local profile does not produce {channel}.", channel
            )
    response.channel_provenance = {"text": "derived", "blocks": "native", "block_bbox": "native"}
    return response, origins
