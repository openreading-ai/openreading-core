"""Run the configured Docling converter and retain measured page-origin diagnostics.

Only local bytes enter conversion. PDFium preflight distinguishes encrypted inputs,
while the standard pipeline supplies item order and physical-page provenance.
"""

from __future__ import annotations

import io

from openreading.adapters.docling_local.config import LocalDoclingConfig


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

    def convert(self, data: bytes) -> dict:
        from docling.datamodel.base_models import DocumentStream

        from openreading.adapters.docling_local.pipeline import create_converter

        self.config.validate_assets()
        if self._converter is None:
            self._converter = create_converter(self.config)
        result = self._converter.convert(DocumentStream(name="source.pdf", stream=io.BytesIO(data)))
        if result.status.value not in {"success", "partial_success"}:
            raise ValueError("Local conversion failed.")
        origins = {}
        for page in result.pages:
            if page.predictions.layout is None:
                origins[str(page.page_no)] = "mixed"
                continue
            flags = {
                cell.from_ocr
                for cluster in page.predictions.layout.clusters
                for cell in cluster.cells
            }
            origins[str(page.page_no)] = (
                "native" if flags == {False} else "ocr" if flags == {True} else "mixed"
            )
        document = result.document.export_to_dict()
        return {
            "pages": document["pages"],
            "items": [item.model_dump(mode="json") for item, _ in result.document.iterate_items()],
            "page_origins": origins,
        }
