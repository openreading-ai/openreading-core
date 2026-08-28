"""GFM/HTML → plain-text projections and markdown escaping (DESIGN §5).

`md_to_text` uses paired-delimiter emphasis only — never blanket metachar deletion — and treats
fenced code as verbatim, so `snake_case`, `3*4`, and code blocks survive intact (the nuextract
`_md_to_text` corruption class). `html_to_text` preserves table structure (rows→newlines,
cells→tabs) instead of collapsing a grid to one line (the reducto `_html_to_text` bug).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from openreading.derive.tables import html_table_to_table, md_table_to_table, table_to_text

# markdown structural metacharacters (for escape_md / the unescape step)
_MD_META = r"\`*_{}[]()#+-.!|>~"
_ESC_RE = re.compile(r"([" + re.escape(_MD_META) + r"])")
_UNESC_RE = re.compile(r"\\([" + re.escape(_MD_META) + r"])")

_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_ATX_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def escape_md(text: str) -> str:
    """Escape markdown metacharacters in literal text destined for the markdown channel, so
    document content can never be reinterpreted as markup (C3)."""
    return _ESC_RE.sub(r"\\\1", text)


# HTML islands embedded in markdown (the NuMarkdown/qwen shape): a line opening one of these
# block elements starts an island consumed to its matching close tag and handled as HTML.
_ISLAND_RE = re.compile(r"^\s*<(table|figure)\b", re.IGNORECASE)
# closed inline HTML pairs in prose (`<b>bold</b>`); a bare `<` (e.g. "3 < 4") is data, untouched.
_INLINE_HTML_RE = re.compile(r"<(b|strong|i|em|u|sub|sup)\b[^>]*>(.*?)</\1\s*>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _html_island(lines: list[str], start: int, tag: str) -> tuple[str, int]:
    """Consume lines[start:] until `tag`'s matching close (nesting-aware). Returns the island
    source and the index after it; an unclosed island degrades to consuming to EOF."""
    open_re = re.compile(rf"<{tag}\b", re.IGNORECASE)
    close_re = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
    depth = 0
    for j in range(start, len(lines)):
        depth += len(open_re.findall(lines[j])) - len(close_re.findall(lines[j]))
        if depth <= 0:
            return "\n".join(lines[start : j + 1]), j + 1
    return "\n".join(lines[start:]), len(lines)


def _inline(text: str) -> str:
    text = _INLINE_HTML_RE.sub(r"\2", text)  # <b>bold</b> -> bold (closed pairs only)
    text = _BR_RE.sub(" ", text)
    text = re.sub(r"(?<!\\)!\[([^\]]*)\]\([^)]*\)", r"\1", text)  # ![alt](url) -> alt
    text = re.sub(r"(?<!\\)\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [text](url) -> text
    text = re.sub(r"(?<!\\)\*\*(.+?)\*\*", r"\1", text)  # **bold**
    text = re.sub(r"(?<!\\)__(.+?)__", r"\1", text)  # __bold__
    text = re.sub(r"(?<!\\)\*(\S(?:.*?\S)?)\*", r"\1", text)  # *italic* (paired)
    text = re.sub(r"(?<![\w\\])_(\S(?:.*?\S)?)_(?!\w)", r"\1", text)  # _italic_ at boundaries
    text = re.sub(r"`([^`]+)`", r"\1", text)  # `code`
    return _UNESC_RE.sub(r"\1", text)  # \X -> X (undo escape_md / author escapes)


def _strip_block(line: str) -> str:
    m = _ATX_RE.match(line)
    if m:
        return _inline(m.group(1))
    line = _QUOTE_RE.sub("", line)
    line = _LIST_RE.sub("", line)
    return _inline(line)


def md_to_text(md: str) -> str:
    """GFM → plain UTF-8 (C1/C2). Emphasis stripped only in matched pairs; fenced code emitted
    verbatim; headings/quotes/list markers/link+image syntax removed; pipe tables projected to
    tab-joined rows."""
    out: list[str] = []
    table_buf: list[str] = []
    in_fence = False
    fence = ""

    def flush_table() -> None:
        if not table_buf:
            return
        table = md_table_to_table("\n".join(table_buf))
        if table is not None:
            out.append(table_to_text(table))
        else:  # not a real table — treat each buffered line as prose
            out.extend(_strip_block(ln) for ln in table_buf)
        table_buf.clear()

    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        fence_tok = _FENCE_RE.match(line)
        if in_fence:
            if fence_tok and line.strip().startswith(fence):
                in_fence = False
            else:
                out.append(line)  # verbatim — no transforms inside a fence
            i += 1
            continue
        if fence_tok:
            flush_table()
            in_fence = True
            fence = fence_tok.group(1)
            i += 1
            continue
        island = _ISLAND_RE.match(line)
        if island:
            # HTML island (NuMarkdown embeds tables/figures as HTML, not pipes): project the
            # CELLS/text, never the markup (C1). A pure image figure contributes nothing.
            flush_table()
            html, i = _html_island(lines, i, island.group(1).lower())
            if island.group(1).lower() == "table":
                t = html_table_to_table(html)
                out.append(table_to_text(t) if t is not None else html_to_text(html))
            else:
                txt = html_to_text(html)
                if txt.strip():
                    out.append(txt)
            continue
        if "|" in line and line.strip():
            table_buf.append(line)
            i += 1
            continue
        flush_table()
        out.append(_strip_block(line))
        i += 1

    flush_table()
    return "\n".join(out)


_BLOCK_TAGS = {
    "p",
    "div",
    "li",
    "ul",
    "ol",
    "table",
    "blockquote",
    "section",
    "article",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pre",
    "figure",
    "figcaption",
}


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # entities → text
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "tr":
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append("\t")
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(html: str) -> str:
    """Tag-strip HTML with structure preserved: block elements → newlines, `<tr>` → newline,
    `<td>/<th>` → tab, entities unescaped. A grid never collapses to a single line."""
    parser = _TextHTMLParser()
    parser.feed(html)
    parser.close()
    raw = "".join(parser.parts)
    lines: list[str] = []
    for line in raw.split("\n"):
        cells = [" ".join(cell.split()) for cell in line.split("\t")]
        rendered = "\t".join(cells).strip("\t")
        if rendered.strip():
            lines.append(rendered)
    return "\n".join(lines)
