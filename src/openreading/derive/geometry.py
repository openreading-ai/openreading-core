"""Geometry/offset helpers (DESIGN §5)."""

from __future__ import annotations


def utf8_slice(text: str, start: int, end: int) -> str:
    """Slice `text` by UTF-8 BYTE offsets (not code points). Providers like Google Document AI
    report textAnchor indices as byte offsets; slicing by code points garbles every document
    after its first multibyte character. A split multibyte boundary decodes with `errors='replace'`
    rather than raising."""
    return text.encode("utf-8")[start:end].decode("utf-8", errors="replace")
