"""Preflight the local proof's native PDF boundary before full extraction.

This helper does not alter the adapter descriptor or add a router capability branch.
It refuses password-protected input and counts physical pages before text extraction.
"""

from pathlib import Path


def preflight_pdf(path: Path) -> tuple[int, bool]:
    import pymupdf

    with pymupdf.open(path) as document:
        if not document.is_pdf:
            raise ValueError("not_pdf")
        return len(document), bool(document.needs_pass)
