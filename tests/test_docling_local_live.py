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
    assert origins == {1: "native", 2: "none", 3: "none"}
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
    assert origins == {1: "native", 2: "ocr", 3: "none"}
    assert "45 days" in (response.document.pages[1].text or "")


def test_real_wrapped_compounds_remain_searchable_with_exact_citations(tmp_path):
    import pymupdf

    from openreading.adapters.docling_local.config import LocalDoclingConfig
    from openreading.artifacts.limits import DoclingLimits, ProfileConfig
    from openreading.artifacts.service import ArtifactService

    source = tmp_path / "input"
    source.mkdir()
    with pymupdf.open() as doc:
        page = doc.new_page()
        for index, line in enumerate(
            ["A third-", "party beneficiary and any non-", "compete duty survive re-", "newal."]
        ):
            page.insert_text((72, 100 + index * 14), line)
        doc.save(source / "wrapped.pdf")
    config = ProfileConfig(
        source,
        tmp_path / "store",
        DoclingLimits(
            pages=5, deadline_seconds=60, worker_memory_bytes=2**32, worker_idle_seconds=60
        ),
        LocalDoclingConfig(
            _assets(), dependency_lock=Path(__file__).resolve().parents[1] / "uv.lock"
        ),
    )
    service = ArtifactService(config)
    try:
        receipt = service.import_document("wrapped.pdf")
        for query in ("third-party", "party", "non-compete", "compete", "renewal", "thirdparty"):
            hits = service.search(receipt.artifact_id, query).hits
            assert len(hits) == 1, query
            passage = service.read(receipt.artifact_id, [hits[0].evidence_id]).passages[0]
            assert hits[0].page == passage.page == 1
            assert hits[0].excerpt == passage.text[hits[0].excerpt_start : hits[0].excerpt_end]
            assert "third-\nparty" in passage.text
            assert "non-\ncompete" in passage.text
    finally:
        service.close()


def test_repeated_real_api_conversions_reuse_native_session_with_bounded_growth(
    tmp_path, monkeypatch
):
    import psutil

    from openreading import run
    from openreading.adapters.docling_local import client, pipeline

    monkeypatch.setenv("DOCLING_LOCAL_ASSETS", str(_assets()))
    monkeypatch.setattr(client, "_shared", client._SharedClient())
    create = pipeline.create_converter
    initialized = 0

    def measured(config):
        nonlocal initialized
        initialized += 1
        return create(config)

    monkeypatch.setattr(pipeline, "create_converter", measured)
    data = _pdf(tmp_path / "source.pdf")
    rss = []
    process = psutil.Process()
    try:
        for index in range(25):
            response = run(data, backend="docling_local", mime_type="application/pdf")
            assert response["status"]["state"] == "succeeded"
            assert "60 days" in response["document"]["text"]
            if index >= 4:
                rss.append(process.memory_info().rss)
        assert initialized == 1
        # Compare post-warmup growth rather than platform-dependent total interpreter RSS.
        assert max(rss) - rss[0] < 200 * 1024**2
    finally:
        client._shared._discard()


def test_real_request_ocr_modes_read_scans_and_force_native_pages(tmp_path, monkeypatch):
    from openreading import run
    from openreading.adapters.docling_local import client, pipeline

    tesseract = os.environ.get("DOCLING_LOCAL_TESSERACT")
    tessdata = os.environ.get("DOCLING_LOCAL_TESSDATA")
    if not (tesseract and tessdata):
        pytest.skip("Set local Tesseract executable and tessdata paths")
    monkeypatch.setenv("DOCLING_LOCAL_ASSETS", str(_assets()))
    monkeypatch.setattr(client, "_shared", client._SharedClient())
    create = pipeline.create_converter
    loaded = []
    measured_origins = []
    convert = client.LocalDoclingClient.convert

    def observed_conversion(self, data, **kwargs):
        result = convert(self, data, **kwargs)
        measured_origins.append(result["page_origins"])
        return result

    def observed(config):
        converter = create(config)
        loaded.append(converter)
        return converter

    monkeypatch.setattr(pipeline, "create_converter", observed)
    monkeypatch.setattr(client.LocalDoclingClient, "convert", observed_conversion)
    data = _pdf(tmp_path / "source.pdf")
    try:
        for mode in ("off", "force", "auto", "off", "force", "auto"):
            response = run(
                data, backend="docling_local", mime_type="application/pdf", features={"ocr": mode}
            )
            assert response["status"]["state"] == "succeeded"
            scan = response["document"]["pages"][1].get("text") or ""
            assert ("45 days" in scan) == (mode != "off"), mode
            origins = measured_origins[-1]
            assert origins["1"] == ("ocr" if mode == "force" else "native"), mode
            assert origins["2"] == ("none" if mode == "off" else "ocr"), mode
        assert len(loaded) == 1
        assert len(loaded[0].initialized_pipelines) == 1
    finally:
        client._shared._discard()
