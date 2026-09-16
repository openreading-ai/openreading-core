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


def test_real_docling_merge_gaps_keep_every_proven_span_and_its_geometry():
    from copy import deepcopy
    from types import SimpleNamespace

    from docling.models.stages.reading_order.readingorder_model import ReadingOrderModel
    from docling_core.types.doc import BoundingBox, DocItemLabel, DoclingDocument, ProvenanceItem

    document = DoclingDocument(name="synthetic")
    first_box = BoundingBox(l=10, t=180, r=40, b=170)
    second_box = BoundingBox(l=50, t=40, r=80, b=50, coord_origin="TOPLEFT")
    entry = document.add_text(
        label=DocItemLabel.TEXT,
        text="alpha",
        prov=ProvenanceItem(page_no=2, charspan=(0, 5), bbox=first_box),
    )
    original_element = SimpleNamespace(label=DocItemLabel.TEXT)
    continuation = SimpleNamespace(
        label=DocItemLabel.TEXT,
        text="beta",
        page_no=2,
        cluster=SimpleNamespace(bbox=second_box),
        hyperlink=None,
    )
    ReadingOrderModel._merge_elements(None, original_element, continuation, entry, 200)
    payload = entry.model_dump(mode="json")
    before = deepcopy(payload)
    assert payload["text"] == "alpha beta"
    assert [p["charspan"] for p in payload["prov"]] == [[0, 5], [6, 10]]
    response, origins = project([payload], {"2": "ocr"})
    blocks = response.document.pages[1].blocks
    assert [b.text for b in blocks] == ["alpha", "beta"]
    assert [b.id for b in blocks] == ["d0-p2-s0", "d0-p2-s6"]
    assert [b.bbox.bbox_native.coords for b in blocks] == [
        [10, 180, 40, 170],
        [50, 160, 80, 150],
    ]
    assert response.document.pages[1].text == "alpha\nbeta"
    assert origins[2] == "ocr"
    assert any(w.code == "text_provenance_fragmented" for w in response.warnings)
    assert not any(w.code == "ambiguous_page_provenance" for w in response.warnings)
    assert payload == before


@pytest.mark.parametrize(
    ("text", "prov"),
    [
        ("alphaXbeta", [{"page_no": 1, "charspan": [0, 5]}, {"page_no": 1, "charspan": [6, 10]}]),
        ("alpha  beta", [{"page_no": 1, "charspan": [0, 5]}, {"page_no": 1, "charspan": [7, 11]}]),
        ("alpha beta", [{"page_no": 1, "charspan": [0, 5]}, {"page_no": 2, "charspan": [6, 10]}]),
        (" alpha", [{"page_no": 1, "charspan": [1, 6]}]),
        ("alpha ", [{"page_no": 1, "charspan": [0, 5]}]),
    ],
)
def test_unattributed_characters_still_report_omission(text, prov):
    response, _ = project([item(text, prov)])
    assert any(w.code == "ambiguous_page_provenance" for w in response.warnings)
    assert not any(w.code == "text_provenance_fragmented" for w in response.warnings)
    assert [b.text for p in response.document.pages for b in p.blocks] == [
        text[p["charspan"][0] : p["charspan"][1]] for p in prov
    ]


def test_fragmentation_does_not_hide_omission_in_another_item():
    response, _ = project(
        [
            item(
                "alpha beta",
                [{"page_no": 1, "charspan": [0, 5]}, {"page_no": 1, "charspan": [6, 10]}],
            ),
            item("bad", [{"page_no": 1, "charspan": [0, 4]}]),
        ]
    )
    codes = {w.code for w in response.warnings}
    assert {"text_provenance_fragmented", "ambiguous_page_provenance"} <= codes
    assert response.document.pages[0].text == "alpha\nbeta"
