"""BL-170: the shared skip-guard for tests and smoke scripts that exercise the real `tesseract`
binary.

`shutil.which("tesseract")` only proves a file exists at that PATH entry — it does not prove the
binary can OCR anything. A broken or partial install (wrong arch, corrupt leptonica, missing
language data) still answers `which` while failing at the actual job: `pixRead: image file not
found` on one machine, a `UnicodeDecodeError` on PNG magic bytes arriving where decoded text was
expected on another (`src/openreading/adapters/tesseract/adapter.py:263`). Both are "present but
broken", and `shutil.which` reports both as "present".

This probe instead runs OCR end-to-end against a tiny in-memory fixture and treats any failure —
an import error, a nonzero exit, undecodable output, or empty text — as "not available", the same
way a real caller would experience it. Lives in the shipped `openreading.testing` package (like
`sample_pdf`) rather than under `tests/` so it can gate both the test suite AND
`scripts/compare_smoke.py`, which runs a live tesseract call inside `make verify`'s own gate — one
place, imported by everything that needs the real binary; do not copy the check.
"""

from __future__ import annotations

import functools


@functools.lru_cache(maxsize=1)
def tesseract_ocr_works() -> bool:
    """True only if the system tesseract binary can OCR pixel data right now.

    Calls `pytesseract.image_to_data(..., output_type=Output.DICT, config="--dpi ...")` — the same
    entry point and shape `_PytesseractRunner.image_to_data` uses in
    `adapters/tesseract/adapter.py` — not `image_to_string`, a distinct pytesseract code path with
    its own tesseract-version parsing and output flags (BL-170 review, Trent). A probe that
    exercises a different path than the code it gates could pass or fail independently of whether
    the real code path works.
    """
    try:
        import pytesseract
        from PIL import Image, ImageDraw, ImageFont
        from pytesseract import Output

        img = Image.new("RGB", (300, 80), "white")
        draw = ImageDraw.Draw(img)
        draw.text((10, 10), "probe", fill="black", font=ImageFont.load_default(size=48))
        data = pytesseract.image_to_data(
            img, lang="eng", output_type=Output.DICT, timeout=10, config="--dpi 150"
        )
    except Exception:  # noqa: BLE001 - a probe reports availability, it never raises
        return False
    return any(text.strip() for text in data.get("text", []))
