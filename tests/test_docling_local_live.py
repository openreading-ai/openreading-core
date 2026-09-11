"""Real local Docling conversion proves the page, origin, and furniture contract end to end.

Every other docling_local test stubs the converter, so this file is the only evidence that the
private CPU pipeline still emits what projection expects. It needs the verified layout assets,
so it runs in the live lane when DOCLING_LOCAL_ASSETS is set. The OCR case also needs
DOCLING_LOCAL_TESSERACT and DOCLING_LOCAL_TESSDATA.
"""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def _assets() -> Path:
    value = os.environ.get("DOCLING_LOCAL_ASSETS")
    if not value:
        pytest.skip("docling_local: set DOCLING_LOCAL_ASSETS to run the live test")
    return Path(value)


def _pdf(path: Path) -> bytes:
    import pymupdf

    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 30), "ACME CONFIDENTIAL HEADER", fontsize=8)
        page.insert_text((72, 100), "Provide notice at least 60 days before renewal.", fontsize=11)
        page.insert_text((280, 820), "Page 1 of 3", fontsize=8)
        with pymupdf.open() as scratch:
            source = scratch.new_page()
            source.insert_text((72, 100), "Invoices are payable within 45 days.", fontsize=14)
            image = source.get_pixmap(dpi=150)
        scanned = doc.new_page()
        scanned.insert_image(scanned.rect, pixmap=image)
        doc.new_page()
        doc.save(path)
    return path.read_bytes()


def _convert(data: bytes, **ocr):
    from openreading.adapters.docling_local.client import LocalDoclingClient
    from openreading.adapters.docling_local.config import LocalDoclingConfig
    from openreading.adapters.docling_local.projection import project_document
    from openreading.artifacts.worker import SETTINGS
    from openreading.types.request import Outputs

    raw = LocalDoclingClient(LocalDoclingConfig(_assets(), **ocr)).convert(data)
    response, origins = project_document(raw, Outputs(**SETTINGS["outputs"]))
    return raw, response, origins


@pytest.fixture(autouse=True)
def _private_cwd(tmp_path, monkeypatch):
    # ONNX Runtime writes a telemetry session file into the working directory.
    monkeypatch.chdir(tmp_path)


def test_real_conversion_keeps_physical_pages_and_discloses_furniture(tmp_path):
    raw, response, origins = _convert(_pdf(tmp_path / "source.pdf"))
    assert list(raw["pages"]) == ["1", "2", "3"]
    assert origins[1] == "native"
    first = response.document.pages[0]
    assert "60 days" in (first.text or "")
    assert "CONFIDENTIAL" not in (response.document.text or "")
    assert {"furniture_text_omitted", "unreadable_pages"} <= {w.code for w in response.warnings}
    assert all(block.bbox is not None and block.bbox.page == 1 for block in first.blocks or [])


def test_real_ocr_labels_the_scanned_page(tmp_path):
    tesseract = os.environ.get("DOCLING_LOCAL_TESSERACT")
    tessdata = os.environ.get("DOCLING_LOCAL_TESSDATA")
    if not (tesseract and tessdata):
        pytest.skip("docling_local: set DOCLING_LOCAL_TESSERACT and DOCLING_LOCAL_TESSDATA")
    _, response, origins = _convert(
        _pdf(tmp_path / "source.pdf"),
        ocr=True,
        tesseract_cmd=Path(tesseract),
        tessdata_path=Path(tessdata),
    )
    assert (origins[1], origins[2]) == ("native", "ocr")
    assert "45 days" in (response.document.pages[1].text or "")
