"""Run the configured Docling converter and retain measured page-origin diagnostics.

Only local bytes enter conversion. PDFium preflight distinguishes encrypted inputs,
while the standard pipeline supplies item order and physical-page provenance.
Body iteration excludes Docling furniture, such as running headers and page numbers.
The client counts furniture items that carry text so projection can disclose the omission.

Ordinary API and HTTP requests share one converter per process through convert_shared.
Conversions are serialized because the pipeline and its native session carry mutable state.
Changed settings or asset hashes evict the existing converter before another request can reuse it.
Failed conversions discard the converter so subsequent requests start with fresh native state.
Replacement collects unreachable native-session cycles before another model is initialized.
Assets are verified on every call. The cache retains no document inputs or conversion results.
Request OCR modes select stages inside the loaded pipeline without replacing its layout session.
Off requests verify model files, while automatic and forced OCR also verify Tesseract assets.
Previously verified OCR hashes remain cached so later OCR requests detect changed language data.
Omitting a request override restores the client's configured OCR default for that conversion.
The supervised artifact worker owns its separate LocalDoclingClient and process lifecycle.
Session eviction does not promise a process-memory ceiling. Repeated asset or configuration changes
can leave native allocations resident even after the previous session has been destroyed.
"""

from __future__ import annotations

import gc
import io
import os
import threading
from dataclasses import replace
from typing import Literal

from openreading.adapters.docling_local.config import LocalDoclingConfig

OcrMode = Literal["auto", "force", "off"]


def _selected_config(config: LocalDoclingConfig, mode: OcrMode | None) -> LocalDoclingConfig:
    if mode is None:
        return config
    if mode not in {"auto", "force", "off"}:
        raise ValueError("Unknown local OCR mode.")
    return replace(config, ocr=mode != "off")


def preflight_pdf(source) -> tuple[int, bool]:
    import pypdfium2 as pdfium

    try:
        with pdfium.PdfDocument(source) as document:
            return len(document), False
    except pdfium.PdfiumError as error:
        if error.err_code == 4:
            return 0, True
        raise ValueError("Unreadable PDF input.") from None


class LocalDoclingClient:
    def __init__(self, config: LocalDoclingConfig):
        self.config = config
        self._converter = None
        self._ocr_mode: OcrMode | None = None

    def convert(self, data: bytes, *, ocr_mode: OcrMode | None = None) -> dict:
        from docling.datamodel.base_models import DocumentStream
        from docling_core.types.doc.common.content_layer import ContentLayer

        from openreading.adapters.docling_local.pipeline import create_converter

        selected = _selected_config(self.config, ocr_mode)
        selected.validate_assets()
        if self._converter is None:
            self._converter = create_converter(selected)
        if ocr_mode != self._ocr_mode:
            self._converter.set_ocr_mode(ocr_mode or ("auto" if self.config.ocr else "off"))
            self._ocr_mode = ocr_mode
        result = self._converter.convert(DocumentStream(name="source.pdf", stream=io.BytesIO(data)))
        if result.status.value not in {"success", "partial_success"}:
            raise ValueError("Local conversion failed.")
        origins = {}
        for page in result.pages:
            if page.predictions.layout is None:
                origins[str(page.page_no)] = "unknown"
                continue
            flags = {
                cell.from_ocr
                for cluster in page.predictions.layout.clusters
                for cell in cluster.cells
            }
            origins[str(page.page_no)] = (
                "none"
                if not flags
                else "native"
                if flags == {False}
                else "ocr"
                if flags == {True}
                else "mixed"
            )
        document = result.document.export_to_dict()
        furniture = result.document.iterate_items(included_content_layers={ContentLayer.FURNITURE})
        return {
            "partial": result.status.value == "partial_success",
            "pages": document["pages"],
            "items": [item.model_dump(mode="json") for item, _ in result.document.iterate_items()],
            "page_origins": origins,
            "omitted_furniture_items": sum(
                1 for item, _ in furniture if getattr(item, "text", None)
            ),
        }


class _SharedClient:
    def __init__(self):
        self.pid = os.getpid()
        self.lock = threading.Lock()
        self.client: LocalDoclingClient | None = None
        self.assets: dict[str, str] | None = None

    def _discard(self):
        if self.client is not None:
            # Native allocations do not trigger Python's cyclic garbage collector promptly.
            # Drop the converter even if a failed conversion's traceback still holds the client.
            self.client._converter = None
            self.client = None
            self.assets = None
            gc.collect()

    def convert(
        self, config: LocalDoclingConfig, data: bytes, *, ocr_mode: OcrMode | None = None
    ) -> dict:
        if self.pid != os.getpid():
            # A fork can inherit a locked mutex and a session owned by the parent process.
            self.__init__()
        with self.lock:
            try:
                assets = _selected_config(config, ocr_mode).validate_assets()
                # Off-mode validation omits OCR files. Keep their last verified hashes so an
                # unchanged off/auto/force sequence reuses layout, while changed files evict it.
                known_assets = self.assets or {}
                changed = any(
                    key in known_assets and known_assets[key] != value
                    for key, value in assets.items()
                )
                if self.client is None or self.client.config != config or changed:
                    self._discard()
                    self.client = LocalDoclingClient(config)
                self.assets = {**(self.assets or {}), **assets}
                if ocr_mode is None:
                    return self.client.convert(data)
                return self.client.convert(data, ocr_mode=ocr_mode)
            except BaseException:
                self._discard()
                raise


_shared = _SharedClient()


def convert_shared(
    config: LocalDoclingConfig, data: bytes, *, ocr_mode: OcrMode | None = None
) -> dict:
    """Convert through the process's single bounded, serialized native session."""
    return _shared.convert(config, data, ocr_mode=ocr_mode)
