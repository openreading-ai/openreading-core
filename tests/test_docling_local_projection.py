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
