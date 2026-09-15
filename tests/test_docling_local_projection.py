"""Local Docling evidence uses all proven page spans and never guesses attribution."""

import pytest

from openreading.types.request import Outputs


def project(items, origins=None):
    from openreading.adapters.docling_local.projection import project_document

    return project_document(
        {
            "items": items,
            "pages": {
                "1": {"size": {"width": 100, "height": 200}},
                "2": {"size": {"width": 100, "height": 200}},
            },
            "page_origins": origins or {"1": "native", "2": "native"},
        },
        Outputs(),
    )


def item(text="alpha beta", prov=None):
    return {
        "text": text,
        "label": "text",
        "prov": prov
        if prov is not None
        else [
            {"page_no": 1, "charspan": [0, len(text)]},
        ],
    }


def test_cross_page_item_splits_only_explicit_nonoverlapping_spans():
    response, origins = project(
        [
            item(
                prov=[
                    {"page_no": 1, "charspan": [0, 6]},
                    {"page_no": 2, "charspan": [6, 10]},
                ]
            )
        ],
        {"1": "native", "2": "ocr"},
    )
    assert [page.text for page in response.document.pages] == ["alpha ", "beta"]
    assert origins == {1: "native", 2: "ocr"}
    assert response.document.pages[0].blocks[0].bbox is None


@pytest.mark.parametrize(
    "prov",
    [
        [],
        [{"charspan": [0, 10]}],
        [{"page_no": 3, "charspan": [0, 10]}],
        [{"page_no": 1, "charspan": [0, 10]}, {"page_no": 2, "charspan": [0, 10]}],
        [{"page_no": 1, "charspan": [-1, 10]}],
        [{"page_no": 1, "charspan": [0, 11]}],
    ],
)
def test_unproven_provenance_omits_text_instead_of_assigning_page_one(prov):
    response, _ = project([item(prov=prov)])
    assert not response.document.text
    assert any(w.code == "ambiguous_page_provenance" for w in response.warnings)


def test_known_bottom_left_geometry_and_mixed_origin_are_preserved():
    response, origins = project(
        [
            item(
                prov=[
                    {
                        "page_no": 2,
                        "charspan": [0, 10],
                        "bbox": {
                            "l": 10,
                            "b": 150,
                            "r": 60,
                            "t": 180,
                            "coord_origin": "BOTTOMLEFT",
                        },
                    }
                ]
            )
        ],
        {"2": "mixed"},
    )
    block = response.document.pages[1].blocks[0]
    assert (block.bbox.page, block.bbox.x, block.bbox.y, block.bbox.w, block.bbox.h) == (
        2,
        0.1,
        0.1,
        0.5,
        0.15,
    )
    assert origins[2] == "mixed"


def test_disabled_table_structure_and_unknown_geometry_are_disclosed():
    response, _ = project(
        [
            {"label": "table", "prov": [{"page_no": 1, "charspan": [0, 0]}]},
            item(prov=[{"page_no": 1, "charspan": [0, 10], "bbox": {"coord_origin": "UNKNOWN"}}]),
        ]
    )
    assert response.document.pages[0].blocks[0].bbox is None
    assert any(w.code == "table_text_unavailable" for w in response.warnings)


def test_upstream_partial_state_is_not_reported_as_complete():
    from openreading.adapters.docling_local.projection import project_document
    from openreading.types.request import Outputs

    response, _ = project_document({"pages": {"1": {}}, "items": [], "partial": True}, Outputs())
    assert response.status.state == "partial"
    assert any(w.code == "partial_conversion" for w in response.warnings)


def test_excluded_furniture_text_is_disclosed_not_silently_dropped():
    from openreading.adapters.docling_local.projection import project_document

    payload = {"pages": {"1": {}}, "items": [item()], "omitted_furniture_items": 2}
    response, _ = project_document(payload, Outputs())
    assert any(w.code == "furniture_text_omitted" for w in response.warnings)
    quiet, _ = project_document({"pages": {"1": {}}, "items": [item()]}, Outputs())
    assert not any(w.code == "furniture_text_omitted" for w in quiet.warnings or [])


def test_blank_pages_and_unmeasured_text_do_not_claim_mixed_origin():
    from openreading.adapters.docling_local.projection import project_document

    response, origins = project_document(
        {"pages": {"1": {}, "2": {}}, "items": [item("visible text")], "page_origins": {}},
        Outputs(),
    )
    assert origins == {1: "unknown", 2: "none"}
    assert response.document.pages[1].text == ""


def test_real_docling_list_marker_text_preserves_original_page_spans():
    from copy import deepcopy

    from docling.models.postprocessing.list_marker_processor import ListItemMarkerProcessor
    from docling_core.types.doc import BoundingBox, DoclingDocument, ProvenanceItem

    document = DoclingDocument(name="synthetic")
    original = "12. Retain this numbered instruction"
    entry = document.add_list_item(
        text=original,
        prov=ProvenanceItem(
            page_no=2,
            charspan=(0, len(original)),
            bbox=BoundingBox(l=10, t=20, r=70, b=40),
        ),
    )
    ListItemMarkerProcessor().process_list_item(entry)
    payload = entry.model_dump(mode="json")
    before = deepcopy(payload)
    assert payload["text"] == "Retain this numbered instruction"
    assert payload["orig"] == original
    assert payload["prov"][0]["charspan"] == [0, len(original)]
    response, origins = project([payload], {"2": "ocr"})
    assert response.document.pages[1].text == original
    block = response.document.pages[1].blocks[0]
    assert block.native_type == "list_item"
    assert block.bbox.page == 2
    assert origins[2] == "ocr"
    assert not any(w.code == "ambiguous_page_provenance" for w in response.warnings or [])
    assert payload == before


def test_list_original_text_uses_all_explicit_spans_without_clamping():
    entry = item(
        "alpha beta", [{"page_no": 1, "charspan": [0, 9]}, {"page_no": 2, "charspan": [9, 13]}]
    )
    entry.update(label="list_item", orig="1. alpha beta", marker="1.")
    response, _ = project([entry])
    assert [page.text for page in response.document.pages] == ["1. alpha ", "beta"]
    entry["prov"][1]["charspan"][1] = 14
    response, _ = project([entry])
    assert not response.document.text
    assert any(w.code == "ambiguous_page_provenance" for w in response.warnings)


@pytest.mark.parametrize("original", [None, "", 123])
def test_list_without_usable_original_keeps_valid_text(original):
    entry = item()
    entry.update(label="list_item", orig=original)
    response, _ = project([entry])
    assert response.document.pages[0].text == "alpha beta"


def test_ordinary_text_does_not_switch_to_original_representation():
    entry = item()
    entry["orig"] = "unrelated original representation"
    response, _ = project([entry])
    assert response.document.pages[0].text == "alpha beta"
