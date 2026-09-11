"""Native conversion diagnostics preserve Docling's one-based physical page numbers."""

from types import SimpleNamespace

from openreading.adapters.docling_local.client import LocalDoclingClient
from openreading.adapters.docling_local.config import LocalDoclingConfig


def test_origin_mapping_uses_physical_page_numbers_and_measured_cells(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})

    class Document:
        def export_to_dict(self):
            return {"pages": {"1": {}, "2": {}, "3": {}, "4": {}}}

        def iterate_items(self, included_content_layers=None):
            return []

    def page(number, flags):
        layout = (
            None
            if flags is None
            else SimpleNamespace(
                clusters=[SimpleNamespace(cells=[SimpleNamespace(from_ocr=f) for f in flags])]
            )
        )
        return SimpleNamespace(page_no=number, predictions=SimpleNamespace(layout=layout))

    result = SimpleNamespace(
        status=SimpleNamespace(value="success"),
        document=Document(),
        pages=[page(1, [False]), page(2, [True]), page(3, [False, True]), page(4, None)],
    )

    class Converter:
        def convert(self, source):
            assert source.stream.getvalue() == b"%PDF"
            return result

    client = LocalDoclingClient(LocalDoclingConfig(tmp_path))
    client._converter = Converter()
    assert client.convert(b"%PDF")["page_origins"] == {
        "1": "native",
        "2": "ocr",
        "3": "mixed",
        "4": "mixed",
    }


def test_pdfium_preflight_rejects_corrupt_input_and_counts_real_pdf(tmp_path):
    import pytest

    from openreading.adapters.docling_local.client import preflight_pdf
    from openreading.testing.sample_pdf import build_sample_pdf

    path = tmp_path / "test.pdf"
    path.write_bytes(build_sample_pdf())
    assert preflight_pdf(path)[0] > 0
    path.write_bytes(b"%PDF-invalid")
    with pytest.raises(ValueError, match="Unreadable"):
        preflight_pdf(path)


def test_client_initializes_once_and_refuses_failed_conversion(tmp_path, monkeypatch):
    import pytest

    from openreading.adapters.docling_local import pipeline

    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})
    created = []
    result = SimpleNamespace(status=SimpleNamespace(value="failure"))

    def create(config):
        created.append(config)
        return SimpleNamespace(convert=lambda source: result)

    monkeypatch.setattr(pipeline, "create_converter", create)
    client = LocalDoclingClient(LocalDoclingConfig(tmp_path))
    for _ in range(2):
        with pytest.raises(ValueError, match="conversion failed"):
            client.convert(b"%PDF")
    assert len(created) == 1


def test_furniture_text_is_counted_for_disclosure(tmp_path, monkeypatch):
    from docling_core.types.doc.common.content_layer import ContentLayer

    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})

    class Document:
        def export_to_dict(self):
            return {"pages": {"1": {}}}

        def iterate_items(self, included_content_layers=None):
            if included_content_layers == {ContentLayer.FURNITURE}:
                return [(SimpleNamespace(text="Page 1 of 3"), 0), (SimpleNamespace(text=""), 0)]
            return []

    result = SimpleNamespace(status=SimpleNamespace(value="success"), document=Document(), pages=[])
    client = LocalDoclingClient(LocalDoclingConfig(tmp_path))
    client._converter = SimpleNamespace(convert=lambda source: result)
    assert client.convert(b"%PDF")["omitted_furniture_items"] == 1
