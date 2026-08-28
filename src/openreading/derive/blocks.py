"""Markdown → typed blocks, and position-based reading order (DESIGN §5, §4.3).

`md_to_blocks` lets markdown/LLM-only backends (anthropic, nuextract, qwen markdown-mode) reach a
`D` blocks grade without fabricating geometry — the blocks are bbox-less and the caller wraps them
in the §4.3 container rule (one synthetic Page + a page_attribution_unavailable warning).
`order_by_position` fixes P5 (tables appended after all paragraphs) by interleaving on provider
span offsets when known, else bbox position.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from openreading.derive.tables import html_table_to_table, md_table_to_table, table_to_text
from openreading.derive.text import (
    _FENCE_RE,
    _ISLAND_RE,
    _LIST_RE,
    _html_island,
    _inline,
    html_to_text,
)
from openreading.types import Block, BlockType

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def md_to_blocks(md: str) -> list[Block]:
    """Line-level GFM segmentation into typed, bbox-less blocks with reading_order: h1→TITLE,
    h2+→SECTION_HEADER, paragraph→TEXT, fenced code→CODE (verbatim), pipe-table group→TABLE (with
    the canonical grid), list line→LIST_ITEM."""
    blocks: list[Block] = []

    def add(btype: BlockType, text: str | None, table: object | None = None) -> None:
        blocks.append(
            Block(type=btype, text=(text or None), reading_order=len(blocks), table=table)  # type: ignore[arg-type]
        )

    para: list[str] = []
    table_buf: list[str] = []
    fence_buf: list[str] = []
    in_fence = False
    fence = ""

    def flush_para() -> None:
        if para:
            add(BlockType.TEXT, _inline(" ".join(ln.strip() for ln in para)))
            para.clear()

    def flush_table() -> None:
        # A run of buffered `|`-bearing lines is only ever a *candidate* table: shape decides,
        # not content — a bare pipe (inline `a | b` shell/OR notation, a stray comparison) often
        # never resolves to a real GFM table. On a real table, close out whatever paragraph
        # preceded it, then emit TABLE. On a non-table, rejoin the candidate lines with `para`
        # instead of emitting a second, spurious TEXT block: every call site invokes flush_table()
        # immediately before flush_para(), so the reunited paragraph then flushes as one block —
        # mirroring md_to_text's sibling fallback (`out.extend(...)` rather than a separate emit).
        if not table_buf:
            return
        table = md_table_to_table("\n".join(table_buf))
        if table is not None:
            flush_para()
            add(BlockType.TABLE, table_to_text(table), table=table)
        else:
            para.extend(table_buf)
        table_buf.clear()

    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        fence_tok = _FENCE_RE.match(line)
        if in_fence:
            if fence_tok and line.strip().startswith(fence):
                add(BlockType.CODE, "\n".join(fence_buf))
                fence_buf.clear()
                in_fence = False
            else:
                fence_buf.append(line)
            i += 1
            continue
        if fence_tok:
            flush_table()
            flush_para()
            in_fence = True
            fence = fence_tok.group(1)
            i += 1
            continue
        island = _ISLAND_RE.match(line)
        if island:
            # HTML island (the NuMarkdown shape): a <table> becomes a TABLE block with the
            # canonical grid; a <figure> becomes a FIGURE block (figcaption text if any, never
            # markup and never a fabricated caption). See derive.text._html_island.
            flush_table()
            flush_para()
            html, i = _html_island(lines, i, island.group(1).lower())
            if island.group(1).lower() == "table":
                table = html_table_to_table(html)
                if table is not None:
                    add(BlockType.TABLE, table_to_text(table), table=table)
                else:
                    add(BlockType.TEXT, html_to_text(html) or None)
            else:
                add(BlockType.FIGURE, html_to_text(html) or None)
            continue
        if not line.strip():
            flush_table()
            flush_para()
            i += 1
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_table()
            flush_para()
            btype = BlockType.TITLE if len(heading.group(1)) == 1 else BlockType.SECTION_HEADER
            add(btype, _inline(heading.group(2)))
            i += 1
            continue
        if _LIST_RE.match(line):
            flush_table()
            flush_para()
            add(BlockType.LIST_ITEM, _inline(_LIST_RE.sub("", line).strip()))
            i += 1
            continue
        if "|" in line:
            # Buffer as a table candidate WITHOUT flushing `para` first (contrast the old
            # behavior) — so a candidate that turns out not to be a real table can rejoin the
            # paragraph it interrupted instead of fragmenting it. See flush_table().
            table_buf.append(line)
            i += 1
            continue
        flush_table()
        para.append(line)
        i += 1

    if in_fence and fence_buf:
        add(BlockType.CODE, "\n".join(fence_buf))
    flush_table()
    flush_para()
    return blocks


_INF = float("inf")


def order_by_position(blocks: Sequence[Block], keys: Sequence[int | None]) -> list[Block]:
    """Return blocks in document reading order. `keys[i]` is block i's provider span offset when
    known (azure spans, google textAnchor startIndex), else None. Stable sort by
    (page, span-offset-or-bbox-y, bbox.y, bbox.x); blocks with neither a key nor a bbox keep input
    order. The signature carries span offsets explicitly because Block has no span field."""

    def sort_key(item: tuple[int, Block, int | None]) -> tuple[float, float, float, float, int]:
        idx, block, key = item
        bbox = block.bbox
        page = float(bbox.page) if bbox else _INF
        if key is not None:
            primary = float(key)
        elif bbox is not None:
            primary = bbox.y
        else:
            primary = _INF
        y = bbox.y if bbox else _INF
        x = bbox.x if bbox else _INF
        return (page, primary, y, x, idx)

    triples = [(i, b, k) for i, (b, k) in enumerate(zip(blocks, keys, strict=False))]
    return [b for _, b, _ in sorted(triples, key=sort_key)]
