"""derive.text — GFM/HTML → plain projections + markdown escaping (DESIGN §5, C1/C2/C3).

The key correctness properties: paired-delimiter emphasis only (never blanket `[*_`]` deletion,
which corrupts snake_case and 3*4 — the nuextract bug), fenced code exempt from every transform,
and HTML tables projected with structure (tabs/newlines), never collapsed to one line (the
reducto `_html_to_text` bug)."""

from __future__ import annotations

from openreading.derive import escape_md, html_to_text, md_to_text


def test_md_to_text_headings_and_paired_emphasis():
    assert md_to_text("# Account Summary") == "Account Summary"
    assert md_to_text("**bold** and *italic* and `code`") == "bold and italic and code"
    assert md_to_text("__b__ and _i_") == "b and i"


def test_md_to_text_never_deletes_unpaired_metachars():
    # the nuextract corruption class: lone * and _ inside words must survive
    assert md_to_text("3*4=12") == "3*4=12"
    assert md_to_text("snake_case_name stays") == "snake_case_name stays"


def test_md_to_text_fence_content_is_verbatim():
    md = "```\nx = a_b * c  # not a heading | not a table\n```"
    # inside a fence: no emphasis strip, no heading strip, no table transform
    assert md_to_text(md) == "x = a_b * c  # not a heading | not a table"


def test_md_to_text_strips_list_and_quote_and_link_markers():
    assert md_to_text("- item one\n- item two") == "item one\nitem two"
    assert md_to_text("> quoted line") == "quoted line"
    assert md_to_text("see [the docs](https://x.example) here") == "see the docs here"
    assert md_to_text("![alt text](img.png)") == "alt text"


def test_md_to_text_pipe_table_becomes_tab_joined_rows():
    md = "| Region | Revenue |\n| --- | --- |\n| North | 4400 |"
    assert md_to_text(md) == "Region\tRevenue\nNorth\t4400"


def test_html_to_text_preserves_table_structure_not_collapsed():
    html = "<p>Hello</p><table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"
    assert html_to_text(html) == "Hello\na\tb\nc\td"


def test_html_to_text_unescapes_entities_and_strips_tags():
    assert html_to_text("<b>Tom &amp; Jerry</b>") == "Tom & Jerry"


def test_escape_md_escapes_structural_metachars():
    out = escape_md("a|b *x* #h [l](u)")
    for ch in ("\\|", "\\*", "\\#", "\\[", "\\]", "\\(", "\\)"):
        assert ch in out
    # escaping is reversible in the sense that md_to_text of escaped text returns the literal
    assert md_to_text(escape_md("3*4 and snake_case")) == "3*4 and snake_case"


# --- HTML islands in markdown (the NuMarkdown shape, confirmed live 2026-07-29) ----------------


_NUMD = (
    '<figure data-type="image" data-id="img_1">\n'
    '  <img src="img_1.png" alt="Bank Logo"/>\n'
    "</figure>\n"
    "\n"
    "# Account Summary\n"
    "\n"
    "Statement Period Date: 5/1/2014 - 5/31/2014\n"
    "\n"
    "<table>\n"
    "  <thead>\n"
    "    <tr><th><b>Date</b></th><th>Amount</th></tr>\n"
    "  </thead>\n"
    "  <tbody>\n"
    "    <tr><td>05/01</td><td>$53,781.48</td></tr>\n"
    "    <tr><td>05/31</td><td>$73,924.59</td></tr>\n"
    "  </tbody>\n"
    "</table>\n"
)


def test_md_to_text_projects_html_table_islands_as_grid_text():
    # NuMarkdown (and qwen/anthropic markdown) embeds tables as HTML, not pipes. The text channel
    # must carry the CELLS (tab-joined rows), never the markup (C1).
    out = md_to_text(_NUMD)
    assert "<" not in out and ">" not in out  # zero markup in the plain-text channel
    assert "Date\tAmount" in out
    assert "05/01\t$53,781.48" in out
    assert "Account Summary" in out and "Statement Period Date" in out


def test_md_to_text_drops_pure_image_figures():
    out = md_to_text(_NUMD)
    assert "img_1" not in out  # an image with no text contributes nothing (never markup)


def test_md_to_text_strips_closed_inline_html_pairs():
    assert md_to_text("a <b>bold</b> and <i>ital</i> word") == "a bold and ital word"
    assert md_to_text("keep 3 < 4 literal") == "keep 3 < 4 literal"  # bare < is data, not a tag
