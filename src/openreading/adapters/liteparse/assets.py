"""Verify explicitly provisioned Tesseract language data before LiteParse can download any.

LiteParse 2.14.4 downloads a missing traineddata file from tessdata_best's `main` branch whenever
OCR runs, including on a page that already has a text layer. That fetch is invisible to the
caller and swaps a pinned model for whatever the branch holds that day. So OCR requires an
absolute tessdata directory whose file matches a SHA-256 published at an upstream commit named
below. The adapter checks it before any worker starts, and the worker checks it again.

Only English is pinned. Both published variants are accepted, because they are both real
upstream files: tessdata_best is LiteParse's own default, and tessdata_fast is the smaller file
Homebrew's Tesseract ships. They recognize text differently, so the parent sends the verified
hash to the worker for a second check before loading LiteParse.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# language -> variant -> (sha256, byte length, upstream file at a fixed commit)
PINNED_TESSDATA: dict[str, dict[str, tuple[str, int, str]]] = {
    "eng": {
        "tessdata_best": (
            "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba",
            15400601,
            "https://github.com/tesseract-ocr/tessdata_best/blob/e12c65a915945e4c28e237a9b52bc4a8f39a0cec/eng.traineddata",
        ),
        "tessdata_fast": (
            "7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2",
            4113088,
            "https://github.com/tesseract-ocr/tessdata_fast/blob/87416418657359cb625c412a48b6e1d6d41c29bd/eng.traineddata",
        ),
    }
}


class OcrAssetError(ValueError):
    """A fixed, path-free refusal code for missing or unverified OCR data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_tessdata(directory: str | None, language: str) -> dict[str, str]:
    """Return the verified variant and hash, or raise OcrAssetError with a fixed code."""
    variants = PINNED_TESSDATA.get(language)
    if variants is None:
        raise OcrAssetError("ocr_language_unverified")
    if not directory or not Path(directory).is_absolute():
        raise OcrAssetError("ocr_assets_missing")
    path = Path(directory) / f"{language}.traineddata"
    try:
        if not path.is_file():
            raise OcrAssetError("ocr_assets_missing")
        size = path.stat().st_size
        # A file of the wrong length cannot match, so the 15 MB hash is skipped for it.
        candidates = {name: pin for name, pin in variants.items() if pin[1] == size}
        digest = file_sha256(path) if candidates else None
    except OSError:
        raise OcrAssetError("ocr_assets_missing") from None
    for name, (sha256, _length, _source) in candidates.items():
        if digest == sha256:
            return {"language": language, "variant": name, "sha256": sha256}
    raise OcrAssetError("ocr_assets_unverified")
