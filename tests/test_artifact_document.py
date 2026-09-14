"""Whole-result retrieval preserves every normalized value without relying on search.

The reconstruction consumer is independent of the server's fragmentation algorithm.
Synthetic retained records exercise channels the slim import profile cannot produce.
"""

import dataclasses
import hashlib
import json

import pytest

from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import (
    ArtifactManifest,
    EngineIdentity,
    FileRecord,
    artifact_id,
    json_bytes,
)
from openreading.artifacts.passages import iter_passages
from openreading.artifacts.service import ArtifactService
from openreading.types.response import NormalizedResponse


def retain(tmp_path, response, origins=None, *, source=b"synthetic source, never a user document"):
    """Publish a synthetic, hash-bound record through the real store's file contract."""
    root = tmp_path.resolve() / "input"
    root.mkdir(exist_ok=True)
    identity = EngineIdentity(core_version="test", backend_version="test", extraction_settings={})
    service = ArtifactService(ProfileConfig(root, root.parent / "store"), identity=identity)
    parsed = NormalizedResponse.model_validate(response)
    passages = list(iter_passages(parsed, origins))
    digest = hashlib.sha256(source).hexdigest()
    identifier = artifact_id(digest, identity)
    files = {
        "source.pdf": source,
        "response.json": json_bytes(response),
        "passages.jsonl": b"\n".join(json_bytes(p.wire()) for p in passages) + b"\n",
    }
    manifest = ArtifactManifest(
        artifact_id=identifier,
        document_sha256=digest,
        display_name="synthetic.pdf",
        source_relative_path="private/source.pdf",
        input_grant_sha256=service.store.grant,
        engine=identity,
        page_count=parsed.document.page_count,
        passage_count=len(passages),
        created_at="2026-09-14T00:00:00Z",
        page_origins=origins or {},
        files={
            name: FileRecord(length=len(data), sha256=hashlib.sha256(data).hexdigest())
            for name, data in files.items()
        },
        warnings=["parser_warnings_present"],
    )
    directory = service.store.documents / identifier
    directory.mkdir()
    for name, data in files.items():
        (directory / name).write_bytes(data)
    (directory / "manifest.json").write_bytes(json_bytes(manifest.wire()))
    return service, identifier, passages


def rich_response(text="OCR code: O0-123-界"):
    return {
        "schema_version": "0.3",
        "status": {"state": "partial"},
        "backend": {"id": "synthetic", "type": "oss_library"},
        "document": {
            "page_count": 3,
            "text": text,
            "pages": [
                {"page_number": 1, "text": "", "blocks": []},
                {
                    "page_number": 2,
                    "width": 612,
                    "height": 792,
                    "text": text,
                    "blocks": [
                        {"type": "figure", "id": "picture", "children": ["code"]},
                        {
                            "type": "text",
                            "id": "code",
                            "native_type": "paragraph",
                            "reading_order": 1,
                            "text": text,
                            "bbox": {"page": 2, "x": 0.1, "y": 0.2, "w": 0.6, "h": 0.1},
                        },
                        {
                            "type": "table",
                            "id": "table",
                            "table": {"rows": [["Amount", None], ["total", "123.00"]]},
                        },
                    ],
                },
                {"page_number": 3, "text": "Native end", "blocks": []},
            ],
        },
        "typed_fields": {
            "key/~": {"value": {"null": None, "nested": [0, False, ""]}},
            "backend_raw": {"value": "A real field name, not the excluded envelope"},
        },
        "chunks": [{"id": "chunk", "block_ids": ["code"], "page_span": [2, 2], "text": text}],
        "usage": {"pages_processed": 3, "duration_ms": 123},
        "warnings": [{"code": "partial_conversion", "message": "Synthetic parser warning"}],
        "channel_provenance": {"blocks": "native", "text": "derived"},
        "backend_raw": {"payload": "RAW-MUST-NOT-LEAVE-STORE"},
    }


def reassemble(results):
    root = None
    positions = {}
    fragments = []
    for result in results:
        assert result["fragment_start"] == len(fragments)
        fragments.extend(result["fragments"])
    assert len(fragments) == results[-1]["fragment_count"]
    for fragment in fragments:
        tokens = [s.replace("~1", "/").replace("~0", "~") for s in fragment["path"].split("/")[1:]]
        if "text" in fragment:
            assert fragment["start"] == positions.get(fragment["path"], 0)
            assert fragment["end"] - fragment["start"] == len(fragment["text"])
            positions[fragment["path"]] = fragment["end"]
            value = fragment["text"]
        else:
            value = fragment["value"]
        if not tokens:
            root = value
            continue
        parent = root
        for token in tokens[:-1]:
            parent = parent[int(token)] if isinstance(parent, list) else parent[token]
        key = int(tokens[-1]) if isinstance(parent, list) else tokens[-1]
        if "text" in fragment and fragment["start"]:
            parent[key] += value
        elif isinstance(parent, list) and key == len(parent):
            parent.append(value)
        else:
            parent[key] = value
        if "text" in fragment and fragment["end"] == fragment["length"]:
            assert len(parent[key]) == fragment["length"]
    assert hashlib.sha256(json_bytes(root)).hexdigest() == results[-1]["content_sha256"]
    return root


def get_all(service, identifier):
    cursor = None
    results = []
    seen = set()
    while True:
        result = service.get_document(identifier, cursor=cursor).wire()
        assert len(json_bytes(result)) <= service.config.limits.document_bytes
        assert result["fragments"]
        results.append(result)
        cursor = result["next_cursor"]
        if cursor is None:
            return results
        assert cursor not in seen
        seen.add(cursor)
        assert len(results) < 1000


def test_full_result_preserves_structure_nulls_origins_and_no_raw(tmp_path):
    response = rich_response()
    origins = {"1": "none", "2": "ocr", "3": "native"}
    service, identifier, passages = retain(tmp_path, response, origins)
    results = get_all(service, identifier)
    assert len(results) == 1
    assert results[0]["fragments"][0]["path"] == ""
    content = reassemble(results)
    expected = {k: v for k, v in response.items() if k != "backend_raw"}
    assert content["response"] == expected
    assert content["page_origins"] == origins
    assert content["warnings"] == ["parser_warnings_present"]
    assert len(content["evidence"]) == len(passages)
    citation = content["evidence"][0]
    assert citation["page"] == 2
    assert citation["text_origin"] == "ocr"
    assert citation["source_block_id"] == "code"
    assert (
        service.read(identifier, [citation["evidence_id"]]).passages[0].text
        == response["document"]["text"]
    )
    assert "RAW-MUST-NOT" not in json.dumps(results)
    assert "private/source.pdf" not in json.dumps(results)
    assert service.store.load(identifier)[0].artifact_id == identifier
    service.close()


def test_pagination_restarts_without_losing_unicode_or_large_block_text(tmp_path):
    response = rich_response(('界🙂\\"\n' * 2000) + "OCR-END-90112.00")
    service, identifier, _ = retain(tmp_path, response, {"1": "none", "2": "ocr", "3": "native"})
    config = dataclasses.replace(
        service.config, limits=dataclasses.replace(service.config.limits, document_bytes=2048)
    )
    service.close()
    with_service = ArtifactService(config)
    first = with_service.get_document(identifier).wire()
    assert first["next_cursor"]
    with_service.close()
    restarted = ArtifactService(config)
    assert restarted.get_document(identifier).wire() == first
    results = get_all(restarted, identifier)
    assert any("text" in part for result in results for part in result["fragments"])
    content = reassemble(results)
    response.pop("backend_raw")
    assert content["response"] == response
    restarted.close()


def test_cursors_cannot_cross_artifact_result_cap_or_operation(tmp_path):
    service, identifier, _ = retain(tmp_path, rich_response("large " * 2000))
    service.config = dataclasses.replace(
        service.config, limits=dataclasses.replace(service.config.limits, document_bytes=2048)
    )
    cursor = service.get_document(identifier).next_cursor
    assert cursor
    other = tmp_path / "other"
    other.mkdir()
    second, second_id, _ = retain(other, rich_response("different " * 2000))
    # Different content under the same synthetic identity must still invalidate continuation.
    second.config = dataclasses.replace(second.config, limits=service.config.limits)
    third_root = tmp_path / "third"
    third_root.mkdir()
    third, third_id, _ = retain(third_root, rich_response("large " * 2000), source=b"other source")
    third.config = dataclasses.replace(third.config, limits=service.config.limits)
    assert third_id != identifier
    assert (
        third.get_document(third_id).content_sha256
        == service.get_document(identifier).content_sha256
    )
    with pytest.raises(ArtifactError, match="invalid_cursor"):
        third.get_document(third_id, cursor=cursor)
    third.close()
    for target, token in [(second, cursor), (service, "malformed")]:
        with pytest.raises(ArtifactError, match="invalid_cursor"):
            target.get_document(second_id if target is second else identifier, cursor=token)
    with pytest.raises(ArtifactError, match="invalid_cursor"):
        service.read(identifier, ["p0002-b0000-s0000"], cursor=cursor)
    service.config = dataclasses.replace(
        service.config, limits=dataclasses.replace(service.config.limits, document_bytes=4096)
    )
    with pytest.raises(ArtifactError, match="invalid_cursor"):
        service.get_document(identifier, cursor=cursor)
    service.close()
    second.close()


def test_document_reads_use_verified_store_and_grant(tmp_path):
    service, identifier, _ = retain(tmp_path, rich_response())
    directory = service.store.documents / identifier
    original = (directory / "response.json").read_bytes()
    (directory / "response.json").write_bytes(original + b" ")
    with pytest.raises(ArtifactError, match="artifact_corrupt"):
        service.get_document(identifier)
    (directory / "response.json").write_bytes(original)
    other = tmp_path.resolve() / "other-grant"
    other.mkdir()
    switched = ArtifactService(dataclasses.replace(service.config, input_root=other))
    with pytest.raises(ArtifactError, match="artifact_not_found"):
        switched.get_document(identifier)
    switched.close()
    service.close()


def test_pointer_escaping_and_explicit_null_fragments(tmp_path):
    response = rich_response()
    response["typed_fields"]["key/~"]["value"] = {"null": None, "long": "界" * 2000}
    service, identifier, _ = retain(tmp_path, response)
    service.config = dataclasses.replace(
        service.config, limits=dataclasses.replace(service.config.limits, document_bytes=2048)
    )
    results = get_all(service, identifier)
    fragments = [part for result in results for part in result["fragments"]]
    assert {"path": "/response/typed_fields/key~1~0/value/null", "value": None} in fragments
    assert reassemble(results)["response"]["typed_fields"] == response["typed_fields"]
    service.close()


@pytest.mark.parametrize(
    "cap,key,value",
    [
        (1024, "key", "text"),
        (2048, "x" * 3000, ""),
        (2048, "x" * 3000, {}),
        (2048, "x" * 3000, 1),
        (2048, "x" * 3000, "text"),
    ],
)
def test_unrepresentable_result_refuses_without_truncation(tmp_path, cap, key, value):
    response = rich_response()
    response["typed_fields"]["key/~"]["value"] = {key: value}
    service, identifier, _ = retain(tmp_path, response)
    service.config = dataclasses.replace(
        service.config, limits=dataclasses.replace(service.config.limits, document_bytes=cap)
    )
    with pytest.raises(ArtifactError, match="response_too_large"):
        service.get_document(identifier)
    service.close()


def test_continuation_reuses_plan_but_rechecks_stored_bytes(tmp_path, monkeypatch):
    from openreading.artifacts import document

    service, identifier, _ = retain(tmp_path, rich_response("content " * 2000))
    service.config = dataclasses.replace(
        service.config, limits=dataclasses.replace(service.config.limits, document_bytes=2048)
    )
    first = service.get_document(identifier)
    assert first.next_cursor

    def repeated_preparation(*args, **kwargs):
        pytest.fail("Continuation re-fragmented the whole document")

    original = document._fragments
    monkeypatch.setattr(document, "_fragments", repeated_preparation)
    second = service.get_document(identifier, cursor=first.next_cursor)
    assert second.fragment_start == len(first.fragments)
    # Existing results remain immutable even when a caller edits its returned model.
    first.fragments[0].path = "changed-by-consumer"
    assert service.get_document(identifier).fragments[0].path == ""
    (service.store.documents / identifier / "response.json").write_bytes(b"corrupt")
    with pytest.raises(ArtifactError, match="artifact_corrupt"):
        service.get_document(identifier, cursor=second.next_cursor)
    monkeypatch.setattr(document, "_fragments", original)
    service.close()


def test_large_valid_envelope_refuses_cleanly_instead_of_empty_result(tmp_path):
    service, identifier, _ = retain(tmp_path, rich_response("x" * 2000))
    service.config = dataclasses.replace(
        service.config, limits=dataclasses.replace(service.config.limits, document_bytes=2048)
    )
    path = service.store.documents / identifier / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["display_name"] = "\u0001" * 255
    path.write_bytes(json_bytes(manifest))
    with pytest.raises(ArtifactError, match="response_too_large"):
        get_all(service, identifier)
    service.close()


def test_oversized_plans_are_not_cached_and_close_releases_plan(tmp_path, monkeypatch):
    from openreading.artifacts import document

    service, identifier, _ = retain(tmp_path, rich_response("text " * 100))
    monkeypatch.setattr(document, "PLAN_CACHE_BYTES", 1)
    first = get_all(service, identifier)
    assert service._document_cache.entry is None
    monkeypatch.setattr(document, "PLAN_CACHE_BYTES", 8 * 1024 * 1024)
    assert get_all(service, identifier) == first
    assert service._document_cache.entry is not None
    service.close()
    assert service._document_cache.entry is None
