"""derive.blocks — markdown → typed bbox-less blocks + position-based reading order (DESIGN §5)."""

from __future__ import annotations

from openreading.derive import md_to_blocks, order_by_position
from openreading.types import Block, BlockType
from openreading.types.geometry import BBox


def _types(blocks: list[Block]) -> list[BlockType]:
    return [b.type for b in blocks]


def test_md_to_blocks_typed_segmentation_and_reading_order():
    md = "# Title\n\n## Section\n\nA paragraph of text.\n\n- item one\n- item two"
    blocks = md_to_blocks(md)
    assert _types(blocks) == [
        BlockType.TITLE,
        BlockType.SECTION_HEADER,
        BlockType.TEXT,
        BlockType.LIST_ITEM,
        BlockType.LIST_ITEM,
    ]
    assert blocks[0].text == "Title"
    assert blocks[3].text == "item one"
    assert [b.reading_order for b in blocks] == [0, 1, 2, 3, 4]
    assert all(b.bbox is None for b in blocks)  # markdown-derived → no fabricated geometry


def test_md_to_blocks_fenced_code_is_a_verbatim_code_block():
    md = "before\n\n```\nx = a_b * c\n```\n\nafter"
    blocks = md_to_blocks(md)
    code = [b for b in blocks if b.type is BlockType.CODE]
    assert len(code) == 1
    assert code[0].text == "x = a_b * c"  # verbatim, no emphasis strip


def test_md_to_blocks_pipe_table_becomes_table_block_with_grid():
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
    blocks = md_to_blocks(md)
    assert len(blocks) == 1 and blocks[0].type is BlockType.TABLE
    assert blocks[0].table is not None
    assert blocks[0].table.rows == [["A", "B"], ["1", "2"]]


def test_md_to_blocks_bare_pipe_mid_paragraph_does_not_fragment_the_paragraph():
    # BL-148: a wrapped paragraph where only an interior line carries a bare `|` (an inline
    # shell/OR notation, not a GFM table row) must stay one TEXT block, not fragment into two or
    # three just because md_table_to_table rejects the buffered candidate.
    md = (
        "We support two access modes.\n"
        "Use role=admin | owner to filter by either role.\n"
        "Both are valid values for this parameter."
    )
    blocks = md_to_blocks(md)
    assert _types(blocks) == [BlockType.TEXT]
    assert blocks[0].text == (
        "We support two access modes. Use role=admin | owner to filter by either role. "
        "Both are valid values for this parameter."
    )


def test_md_to_blocks_pipe_table_still_splits_surrounding_paragraphs_correctly():
    # The valid-table path must stay unchanged: a real table between two paragraphs still yields
    # three distinct blocks, with the table's own grid intact.
    md = "intro para\n| A | B |\n| --- | --- |\n| 1 | 2 |\noutro para"
    blocks = md_to_blocks(md)
    assert _types(blocks) == [BlockType.TEXT, BlockType.TABLE, BlockType.TEXT]
    assert blocks[0].text == "intro para"
    assert blocks[1].table is not None
    assert blocks[1].table.rows == [["A", "B"], ["1", "2"]]
    assert blocks[2].text == "outro para"


def _blk(text: str, bbox: BBox | None = None) -> Block:
    return Block(type=BlockType.TEXT, text=text, bbox=bbox)


def _bbox(y: float, x: float = 0.0, page: int = 1) -> BBox:
    return BBox(x=x, y=y, w=0.1, h=0.1, page=page)


def test_order_by_position_uses_provider_span_offsets_when_present():
    blocks = [_blk("third"), _blk("first"), _blk("second")]
    keys = [200, 0, 100]  # provider span offsets
    ordered = order_by_position(blocks, keys)
    assert [b.text for b in ordered] == ["first", "second", "third"]


def test_order_by_position_falls_back_to_bbox_interleave():
    # the P5 fix: a table mid-page must interleave by position, not be appended after paragraphs
    para_top = _blk("intro", _bbox(0.1))
    table_mid = _blk("TABLE", _bbox(0.5))
    para_bot = _blk("outro", _bbox(0.9))
    ordered = order_by_position([para_bot, table_mid, para_top], [None, None, None])
    assert [b.text for b in ordered] == ["intro", "TABLE", "outro"]


def test_order_by_position_stable_when_no_key_and_no_bbox():
    blocks = [_blk("a"), _blk("b"), _blk("c")]
    ordered = order_by_position(blocks, [None, None, None])
    assert [b.text for b in ordered] == ["a", "b", "c"]  # input order preserved (stable)


def test_order_by_position_respects_source_page():
    p2 = _blk("page2", _bbox(0.1, page=2))
    p1 = _blk("page1", _bbox(0.9, page=1))
    ordered = order_by_position([p2, p1], [None, None])
    assert [b.text for b in ordered] == ["page1", "page2"]


def test_md_to_blocks_html_table_island_becomes_table_block_with_grid():
    # The NuMarkdown shape (confirmed live): tables are HTML islands, not pipe tables. They must
    # become TABLE blocks with the canonical grid (html_table_to_table), never TEXT-with-markup.
    md = (
        "intro para\n"
        "\n"
        "<table>\n"
        "<tr><th>Date</th><th>Amount</th></tr>\n"
        "<tr><td>05/01</td><td>$53,781.48</td></tr>\n"
        "</table>\n"
        "\n"
        "outro para\n"
    )
    blocks = md_to_blocks(md)
    types = [b.type for b in blocks]
    assert types == [BlockType.TEXT, BlockType.TABLE, BlockType.TEXT]
    tbl = blocks[1]
    assert tbl.table is not None
    assert tbl.table.rows == [["Date", "Amount"], ["05/01", "$53,781.48"]]
    assert tbl.text == "Date\tAmount\n05/01\t$53,781.48"  # tab-joined projection, no markup


def test_md_to_blocks_figure_island_becomes_textless_figure_block():
    md = '<figure data-type="image"><img src="x.png" alt="Logo"/></figure>\n\nafter\n'
    blocks = md_to_blocks(md)
    assert [b.type for b in blocks] == [BlockType.FIGURE, BlockType.TEXT]
    assert blocks[0].text is None  # no markup, no fabricated text for a pure image
    assert blocks[1].text == "after"
