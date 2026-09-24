"""Exact source spans survive segmentation, missing geometry, and physical page gaps."""

import pytest

from openreading.artifacts.passages import iter_passages
from openreading.types.response import NormalizedResponse


def response(pages):
    return NormalizedResponse.model_validate(
        {
            "status": {"state": "succeeded"},
            "backend": {"id": "pymupdf", "type": "oss_library"},
            "document": {"pages": pages, "page_count": 13},
        }
    )


def test_exact_unicode_segments_and_physical_pages():
    original = "notice 😀 Straße " * 150
    result = list(
        iter_passages(
            response(
                [
                    {"page_number": 1, "text": ""},
                    {"page_number": 13, "blocks": [{"type": "text", "text": original}]},
                ]
            )
        )
    )
    assert len(result) > 1
    assert "".join(item.text for item in result) == original
    for index, item in enumerate(result):
        assert item.text == original[item.text_start : item.text_end]
        assert len(item.text) <= 1024
        assert item.page == 13
        assert item.evidence_id == f"p0013-b0000-s{index:04d}"
        assert item.bbox is None


def test_reading_order_and_fallback_are_deterministic():
    pages = [
        {
            "page_number": 3,
            "text": "fallback",
            "blocks": [
                {"type": "text", "text": "second", "reading_order": 9},
                {"type": "text", "text": "first", "reading_order": 0},
            ],
        },
        {"page_number": 4, "text": "page only"},
    ]
    result = list(iter_passages(response(pages)))
    assert [p.text for p in result] == ["first", "second", "page only"]
    assert result[-1].source_kind == "page_text"
    assert [p.evidence_id for p in result] == [
        "p0003-b0000-s0000",
        "p0003-b0001-s0000",
        "p0004-b0000-s0000",
    ]


def test_textless_origin_produces_no_passage():
    assert list(iter_passages(response([{"page_number": 1, "text": " \n"}]), {"1": "none"})) == []


@pytest.mark.parametrize(
    "page",
    [
        {"page_number": 1, "text": "visible"},
        {"page_number": 1, "blocks": [{"type": "text", "text": "visible"}]},
    ],
)
def test_textless_origin_cannot_hide_retained_text(page):
    with pytest.raises(ValueError, match="contradicts retained text"):
        list(iter_passages(response([page]), {"1": "none"}))


@pytest.mark.parametrize("origin", ["native", "ocr", "mixed", "unknown"])
def test_measured_origin_cannot_label_a_page_without_retained_text(origin):
    pages = [{"page_number": 1, "text": " \n"}, {"page_number": 2, "text": "visible"}]
    with pytest.raises(ValueError, match="without retained text"):
        list(iter_passages(response(pages), {"1": origin, "2": "native"}))


@pytest.mark.parametrize("text", ["", "conflicting text"])
def test_duplicate_physical_pages_refuse_all_passages(text):
    pages = [{"page_number": 1, "text": text}, {"page_number": 1, "text": "visible"}]
    with pytest.raises(ValueError, match="duplicate physical page"):
        next(iter_passages(response(pages)))


@pytest.mark.parametrize("version", ["0.3", "0.4"])
def test_local_legacy_load_rejects_duplicate_pages_with_matching_hashes(tmp_path, version):
    import json

    from openreading.artifacts.limits import ArtifactError
    from openreading.artifacts.models import artifact_id, json_bytes
    from openreading.artifacts.store import file_record
    from tests.test_artifact_jobs import service as service_fixture

    fixture = service_fixture.__wrapped__(tmp_path)
    service = next(fixture)
    try:
        receipt = service.import_document("test.pdf")
        root = service.store.documents / receipt.artifact_id
        data = json.loads((root / "response.json").read_bytes())
        data["document"]["pages"] = [
            {"page_number": 1, "text": "Approved."},
            {"page_number": 1, "text": "Rejected."},
        ]
        parsed = NormalizedResponse.model_validate(data)
        # Emulate a legacy writer that projected each page without cross-page validation.
        passages = []
        for page in parsed.document.pages:
            single = parsed.model_copy(
                update={"document": parsed.document.model_copy(update={"pages": [page]})}
            )
            passages.extend(iter_passages(single))
        (root / "response.json").write_bytes(json_bytes(data))
        (root / "passages.jsonl").write_bytes(
            b"".join(json_bytes(p.wire()) + b"\n" for p in passages)
        )
        manifest = json.loads((root / "manifest.json").read_bytes())
        manifest["format"] = "local-document.v" + version
        manifest["evidence_format"] = "passages.v" + version
        manifest["page_origins"] = {}
        manifest["passage_count"] = len(passages)
        for name in ("response.json", "passages.jsonl"):
            manifest["files"][name] = file_record(root / name, None).wire()
        identifier = artifact_id(
            manifest["document_sha256"],
            service.identity,
            version=version,
            source_file=manifest["source_file"],
        )
        manifest["artifact_id"] = identifier
        (root / "manifest.json").write_bytes(json_bytes(manifest))
        root.rename(service.store.documents / identifier)
        with pytest.raises(ArtifactError, match="artifact_corrupt"):
            service.load_artifact(identifier)
    finally:
        fixture.close()
