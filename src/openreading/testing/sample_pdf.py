"""Content-stable 2-page test PDF, generated exactly like internal/research/openreading/_data/live_runs.md so
local-library adapters can be EXERCISED for real and their geometry asserted against the
empirically-verified coordinates.

Page 1: 20pt title at baseline y=80, a 12pt and a 9pt paragraph, and a 3-col x 4-row table
drawn with cell borders (draw_rect) at bbox (72,280)-(432,392). Page 2: two short text columns
plus a tiny 8x8 PNG at (60,300). US-Letter 612x792 pt. Requires PyMuPDF (dev/test dep).
"""

from __future__ import annotations

_TABLE = [
    ["Region", "Units", "Revenue"],
    ["North", "120", "4400"],
    ["South", "85", "3100"],
    ["West", "42", "1650"],
]
TABLE_BBOX = (72.0, 280.0, 432.0, 392.0)


def build_sample_pdf() -> bytes:
    """Return the test PDF as bytes. Pages, text and geometry are identical every call. The PDF
    trailer /ID is not, so the bytes and their sha256 differ between calls."""
    import fitz  # lazy: PyMuPDF is a dev/test dep, not a core dependency

    doc = fitz.open()
    doc.set_metadata(
        {
            "title": "OpenReading Test Document",
            "author": "OpenReading Research",
            "subject": "Library invocation samples",
            "creationDate": "D:20260721000000Z",
            "modDate": "D:20260721000000Z",
        }
    )

    # --- page 1 ---
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((72, 80), "OpenReading Test Document", fontsize=20, fontname="helv")
    p1.insert_text(
        (72, 130),
        "This is the first paragraph rendered at twelve points. It exercises text extraction.",
        fontsize=12,
        fontname="helv",
    )
    p1.insert_text(
        (72, 170),
        "A second, smaller paragraph at nine points for font-size variation.",
        fontsize=9,
        fontname="helv",
    )
    # 3x4 grid: borders via draw_rect (surfaces as curves, not rects — see live_runs gotcha)
    x0, y0, x1, y1 = TABLE_BBOX
    n_rows, n_cols = 4, 3
    rh = (y1 - y0) / n_rows
    cw = (x1 - x0) / n_cols
    for r in range(n_rows):
        for c in range(n_cols):
            cx0, cy0 = x0 + c * cw, y0 + r * rh
            p1.draw_rect(fitz.Rect(cx0, cy0, cx0 + cw, cy0 + rh), color=(0, 0, 0), width=0.8)
            p1.insert_text((cx0 + 4, cy0 + rh - 8), _TABLE[r][c], fontsize=10, fontname="helv")

    # --- page 2: two columns + image ---
    p2 = doc.new_page(width=612, height=792)
    p2.insert_text((72, 100), "Left column block one.", fontsize=11, fontname="helv")
    p2.insert_text((72, 130), "Left column block two.", fontsize=11, fontname="helv")
    p2.insert_text((330, 100), "Right column block one.", fontsize=11, fontname="helv")
    p2.insert_text((330, 130), "Right column block two.", fontsize=11, fontname="helv")
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8))
    pix.clear_with(128)  # solid mid-gray 8x8
    p2.insert_image(fitz.Rect(60, 300, 124, 364), pixmap=pix)

    data = doc.tobytes()
    doc.close()
    return bytes(data)


def build_scanned_pdf(pages: int = 2) -> bytes:
    """A scan-like PDF: each page is a full-page raster image with NO text layer. The signal
    probe (via pypdf) must see no extractable text + an image → `scanned_pages_detected` and
    `text_source: none`. Page content is stable across calls, the trailer /ID is not. Uses
    PyMuPDF (a dev/test dep) only to BUILD the fixture — the probe under test reads it with
    pypdf, never pymupdf."""
    import fitz  # lazy: PyMuPDF is a dev/test dep, not a core dependency

    doc = fitz.open()
    doc.set_metadata({"title": "OpenReading Scanned Fixture", "creationDate": "D:20260721000000Z"})
    for _ in range(pages):
        page = doc.new_page(width=612, height=792)
        # a deterministic checkerboard-ish gray raster covering the whole page (a "scan")
        pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 51, 66), False)
        for y in range(66):
            for x in range(51):
                pix.set_pixel(x, y, (200 if (x // 6 + y // 6) % 2 else 90,))
        page.insert_image(fitz.Rect(0, 0, 612, 792), pixmap=pix)  # full-page image, no text
    data = doc.tobytes()
    doc.close()
    return bytes(data)


def build_garbled_pdf() -> bytes:
    """A PDF whose text layer is mojibake — selectable-but-garbage (the OCRmyPDF "damaged
    ToUnicode" failure class). The garble composite must score above the 0.3 `garbled: true` cut.
    Page content is stable across calls, the trailer /ID is not."""
    import fitz  # lazy: PyMuPDF is a dev/test dep, not a core dependency

    # latin-1 mojibake: runs of accented/symbol chars with no ASCII-vowel words — renders and
    # re-extracts as the same garbage a broken text layer produces. Includes replacement chars.
    garble = (
        "Ã©Ã¨ÃªÃ«Å â€™Ã±Â§Â¶ Ã Ã¢Ã¤ Ãµ Ã¼Ã¿ â‚¬Â£Â¥ Ã˜Ã† Ã‡Ã‰ ��� Ã¾Ã°Å¸ Â«Â»Â·Â¸ Ã‚Ã„Ã€ Å½Å¡ Ã¦Å¾"
    )
    doc = fitz.open()
    doc.set_metadata({"title": "OpenReading Garbled Fixture", "creationDate": "D:20260721000000Z"})
    page = doc.new_page(width=612, height=792)
    y = 80
    for _ in range(12):
        page.insert_text((72, y), garble, fontsize=11, fontname="helv")
        y += 28
    data = doc.tobytes()
    doc.close()
    return bytes(data)
