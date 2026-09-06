"""PDF page-count derivation (DESIGN §5). Optional in-tree dependency."""

from __future__ import annotations


def pdf_page_count(data: bytes) -> int | None:
    """Exact page count from PDF bytes via an in-tree PDF library (pymupdf). Returns None when no
    library is importable or the bytes are not a parseable PDF — callers fall back to a heuristic
    (e.g. anthropic's distinct-cited-pages) or leave page_count absent."""
    try:
        # Never the `fitz` alias: it prints a deprecation warning to stdout, and stdout
        # carries only the JSON envelope. The quiet `pymupdf` name arrived in 1.24.3, which is
        # the floor this package pins.
        import pymupdf  # type: ignore
    except ImportError:  # pragma: no cover - optional-dep fallback path
        return None
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001 — any parse failure → unknown page count
        return None
    try:
        return int(doc.page_count)
    finally:
        doc.close()
