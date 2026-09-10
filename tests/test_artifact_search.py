"""Retrieval preserves Unicode offsets and binds bounded continuations to requests."""

import pytest

from openreading.artifacts.limits import ArtifactError
from openreading.artifacts.models import (
    ArtifactManifest,
    EngineIdentity,
    FileRecord,
    Passage,
    json_bytes,
)
from openreading.artifacts.search import read, search


def records(texts):
    return [
        Passage(
            evidence_id=f"p0001-b{i:04d}-s0000",
            page=1,
            block_index=i,
            segment_index=0,
            source_kind="block_text",
            text_start=0,
            text_end=len(text),
            text=text,
        )
        for i, text in enumerate(texts)
    ]


@pytest.fixture
def manifest():
    return ArtifactManifest(
        artifact_id="or1_" + "a" * 64,
        document_sha256="b" * 64,
        display_name="test.pdf",
        source_relative_path="test.pdf",
        input_grant_sha256="c" * 64,
        page_count=1,
        passage_count=3,
        engine=EngineIdentity(
            core_commit="d" * 40, core_version="0.3.0", backend_version="1", extraction_settings={}
        ),
        created_at="2026-09-10T00:00:00Z",
        files={
            name: FileRecord(length=1, sha256="a" * 64)
            for name in ["source.pdf", "response.json", "passages.jsonl"]
        },
    )


def test_unicode_casefold_preserves_original_offsets(manifest):
    text = "😀 " * 200 + "Straße renewal"
    result = search(
        manifest, records([text, "renewal", "unrelated"]), "STRASSE renewal", 5, None, 8192
    )
    hit = result.hits[0]
    assert hit.matched_terms == ["strasse", "renewal"]
    assert "Straße" in hit.excerpt
    assert hit.excerpt == text[hit.excerpt_start : hit.excerpt_end]
    assert len(hit.excerpt) <= 240
    assert len(result.hits) == 2


def test_pagination_does_not_drop_hits_and_rejects_other_query(manifest):
    passages = records(["renewal " * 100] * 5)
    first = search(manifest, passages, "renewal", 2, None, 8192)
    second = search(manifest, passages, "renewal", 2, first.next_cursor, 8192)
    last = search(manifest, passages, "renewal", 2, second.next_cursor, 8192)
    assert [h.evidence_id for r in [first, second, last] for h in r.hits] == [
        p.evidence_id for p in passages
    ]
    assert last.next_cursor is None
    with pytest.raises(ArtifactError, match="invalid_cursor"):
        search(manifest, passages, "other", 2, first.next_cursor, 8192)


def test_read_paginates_whole_records_with_unicode_byte_cap(manifest):
    passages = records(["😀" * 1000] * 8)
    ids = [p.evidence_id for p in passages]
    result, found, cursor = None, [], None
    while result is None or cursor:
        result = read(manifest, passages, ids, cursor, 16384)
        assert len(json_bytes(result.wire())) <= 16384
        found.extend(p.evidence_id for p in result.passages)
        cursor = result.next_cursor
    assert found == ids
    with pytest.raises(ArtifactError, match="evidence_not_found"):
        read(manifest, passages, [ids[0], "p0002-b0000-s0000"], None, 16384)
    with pytest.raises(ArtifactError, match="response_too_large"):
        read(manifest, passages, ids, None, 100)


def test_no_match_is_success_and_invalid_cursor_is_sanitized(manifest):
    assert search(manifest, records(["hello"]), "!!!", 5, None, 8192).warnings == ["no_matches"]
    with pytest.raises(ArtifactError, match="invalid_cursor"):
        search(manifest, records(["hello"]), "hello", 5, "secret", 8192)


def test_randomized_utf8_caps_preserve_whole_records_and_terminate(manifest):
    import random

    randomizer = random.Random(20260910)
    for _ in range(60):
        passages = records(
            [
                "notice " + randomizer.choice(["😀", "é", "a", "漢"]) * randomizer.randint(1, 900)
                for _ in range(randomizer.randint(1, 8))
            ]
        )
        ids = [p.evidence_id for p in passages]
        for operation in [
            lambda cursor, cap, passages=passages, ids=ids: read(
                manifest, passages, ids, cursor, cap
            ),
            lambda cursor, cap, passages=passages: search(
                manifest, passages, "notice", 3, cursor, cap
            ),
        ]:
            full = operation(None, 65536)
            for cap in [
                0,
                1,
                len(json_bytes(full.wire())) - 1,
                len(json_bytes(full.wire())),
                16384,
            ]:
                cursor, found = None, []
                for _step in range(len(passages) + 1):
                    try:
                        result = operation(cursor, cap)
                    except ArtifactError as error:
                        assert error.code == "response_too_large"
                        break
                    assert len(json_bytes(result.wire())) <= cap
                    items = result.passages if hasattr(result, "passages") else result.hits
                    assert items
                    found.extend(item.evidence_id for item in items)
                    cursor = result.next_cursor
                    if cursor is None:
                        assert found == ids
                        break
                else:
                    pytest.fail("Pagination did not terminate")


def test_large_evidence_indices_match_the_vendored_schema():
    import jsonschema

    from openreading.schemas import passage_schema

    passage = Passage(
        evidence_id="p10000-b10000-s10000",
        page=10000,
        block_index=10000,
        segment_index=10000,
        source_kind="block_text",
        text_start=0,
        text_end=1,
        text="x",
    )
    jsonschema.validate(passage.wire(), passage_schema())
