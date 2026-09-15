"""Native conversion diagnostics preserve Docling's one-based physical page numbers."""

from types import SimpleNamespace

import pytest

from openreading.adapters.docling_local.client import LocalDoclingClient
from openreading.adapters.docling_local.config import LocalDoclingConfig


def test_origin_mapping_uses_physical_page_numbers_and_measured_cells(tmp_path, monkeypatch):
    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})

    class Document:
        def export_to_dict(self):
            return {"pages": {"1": {}, "2": {}, "3": {}, "4": {}}}

        def iterate_items(self, included_content_layers=None, traverse_pictures=False):
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
        "4": "unknown",
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

        def iterate_items(self, included_content_layers=None, traverse_pictures=False):
            if included_content_layers == {ContentLayer.FURNITURE}:
                return [(SimpleNamespace(text="Page 1 of 3"), 0), (SimpleNamespace(text=""), 0)]
            return []

    result = SimpleNamespace(status=SimpleNamespace(value="success"), document=Document(), pages=[])
    client = LocalDoclingClient(LocalDoclingConfig(tmp_path))
    client._converter = SimpleNamespace(convert=lambda source: result)
    assert client.convert(b"%PDF")["omitted_furniture_items"] == 1


@pytest.fixture
def picture_document():
    from docling_core.types.doc import (
        BoundingBox,
        ContentLayer,
        DocItemLabel,
        DoclingDocument,
        ProvenanceItem,
        Size,
    )

    document = DoclingDocument(name="synthetic-picture-text")
    document.add_page(page_no=1, size=Size(width=200, height=300))
    document.add_page(page_no=2, size=Size(width=200, height=300))

    def provenance(page, text):
        return ProvenanceItem(
            page_no=page,
            charspan=(0, len(text)),
            bbox=BoundingBox(l=10, t=20, r=150, b=40),
        )

    document.add_text(
        label=DocItemLabel.TEXT, text="Before picture", prov=provenance(1, "Before picture")
    )
    picture = document.add_picture(prov=provenance(2, ""))
    nested = document.add_picture(parent=picture, prov=provenance(2, ""))
    for parent, label, text in [
        (picture, DocItemLabel.TEXT, "Nested amount 234.50"),
        (nested, DocItemLabel.CAPTION, "Nested caption"),
    ]:
        document.add_text(label=label, text=text, parent=parent, prov=provenance(2, text))
    document.add_text(
        label=DocItemLabel.PAGE_FOOTER,
        text="Picture footer",
        parent=nested,
        content_layer=ContentLayer.FURNITURE,
        prov=provenance(2, "Picture footer"),
    )
    return document


def convert_document(document, tmp_path, monkeypatch):
    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})
    result = SimpleNamespace(status=SimpleNamespace(value="success"), document=document, pages=[])
    client = LocalDoclingClient(LocalDoclingConfig(tmp_path))
    client._converter = SimpleNamespace(convert=lambda source: result)
    return client.convert(b"%PDF")


def test_picture_children_reach_page_text_once_with_provider_provenance(
    picture_document, tmp_path, monkeypatch
):
    from docling_core.types.doc import ContentLayer

    from openreading.adapters.docling_local.projection import project_document
    from openreading.types.request import Outputs

    payload = convert_document(picture_document, tmp_path, monkeypatch)
    # Compare against the flat export, not the iterator whose defaults hid these children.
    expected = {
        item.self_ref: item.model_dump(mode="json")
        for item in picture_document.texts
        if item.content_layer == ContentLayer.BODY
    }
    actual = [item for item in payload["items"] if item.get("text")]
    assert len(actual) == len(expected)
    assert {item["self_ref"]: item for item in actual} == expected
    assert len(payload["items"]) == len({item["self_ref"] for item in payload["items"]})

    response, _ = project_document(payload, Outputs())
    assert [page.text for page in response.document.pages] == [
        "Before picture",
        "Nested caption\nNested amount 234.50",
    ]
    blocks = response.document.pages[1].blocks
    assert [block.native_type for block in blocks] == ["caption", "text"]
    assert all(block.bbox.page == 2 for block in blocks)
    assert not any(w.code == "unreadable_pages" for w in response.warnings or [])


def test_picture_furniture_is_counted_and_disclosed(picture_document, tmp_path, monkeypatch):
    from docling_core.types.doc import ContentLayer

    from openreading.adapters.docling_local.projection import project_document
    from openreading.types.request import Outputs

    payload = convert_document(picture_document, tmp_path, monkeypatch)
    expected = sum(
        bool(item.text)
        for item in picture_document.texts
        if item.content_layer == ContentLayer.FURNITURE
    )
    assert expected == 1
    assert payload["omitted_furniture_items"] == expected
    response, _ = project_document(payload, Outputs())
    assert "Picture footer" not in response.document.text
    assert any(w.code == "furniture_text_omitted" for w in response.warnings)
