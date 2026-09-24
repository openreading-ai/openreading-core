"""External responses retain exact values and source bindings without local parsing."""

import hashlib
from dataclasses import replace

import pytest

from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.models import EngineIdentity
from openreading.artifacts.service import ArtifactService
from tests.test_artifact_document import reassemble, rich_response


@pytest.fixture
def retention(tmp_path):
    root = tmp_path.resolve() / "input"
    root.mkdir()
    (root / "source.md").write_bytes("synthetic 界".encode())
    identity = EngineIdentity(core_version="test", backend_version="test", extraction_settings={})
    service = ArtifactService(ProfileConfig(root, root.parent / "store"), identity=identity)
    yield service
    service.close()


def retain(service, response, **kwargs):
    from openreading.artifacts.retention import retain_response

    return retain_response(
        service,
        "source.md",
        response,
        source_sha256=hashlib.sha256("synthetic 界".encode()).hexdigest(),
        destination_sha256="d" * 64,
        request_sha256="e" * 64,
        **kwargs,
    )


def complete(service, identifier):
    results, cursor = [], None
    while True:
        page = service.get_document(identifier, cursor).wire()
        results.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return reassemble(results)


def test_external_response_preserves_values_and_records_acquisition(retention, monkeypatch):
    monkeypatch.setattr(retention, "_worker", lambda *args: pytest.fail("must not parse"))
    response = rich_response()
    response["future_channel"] = {"empty": None, "value": [False, 0, ""]}
    receipt = retain(retention, response)
    assert receipt.schema_version == "0.5"
    assert receipt.extraction_state == "partial"
    manifest = retention.load_artifact(receipt.artifact_id)
    assert manifest.format == "local-document.v0.5"
    assert manifest.source_file == "source.md"
    assert manifest.acquisition.destination_sha256 == "d" * 64
    assert manifest.page_origins == {}
    value = complete(retention, receipt.artifact_id)
    assert value["response"] == {k: v for k, v in response.items() if k != "backend_raw"}
    assert all("text_origin" not in evidence for evidence in value["evidence"])
    assert retention.store.load_document(receipt.artifact_id)[2] == response


def test_structured_only_result_has_no_invented_quotes(retention):
    response = rich_response()
    response["document"] = {}
    receipt = retain(retention, response)
    assert receipt.passage_count == 0
    assert receipt.page_count is None
    assert receipt.next_action == "get_document"
    assert (
        complete(retention, receipt.artifact_id)["response"]["typed_fields"]
        == response["typed_fields"]
    )
    assert retention.search(receipt.artifact_id, "amount").hits == []
    with pytest.raises(ArtifactError, match="evidence_not_found"):
        retention.read(receipt.artifact_id, ["d0000-b0000-s0000"])


def test_response_content_changes_identity_and_restart_reads(retention):
    first = retain(retention, rich_response("first"))
    second = retain(retention, rich_response("second"))
    assert first.artifact_id != second.artifact_id
    assert retain(retention, rich_response("first")).reused
    with_service = ArtifactService(retention.config, identity=retention.identity)
    try:
        assert (
            complete(with_service, second.artifact_id)["response"]["document"]["text"] == "second"
        )
    finally:
        with_service.close()


def test_changed_source_cannot_be_bound_to_earlier_upload(retention):
    (retention.config.input_root / "source.md").write_bytes(b"changed")
    with pytest.raises(ArtifactError, match="artifact_corrupt"):
        retain(retention, rich_response())
    assert list(retention.store.documents.iterdir()) == []
    assert list((retention.config.artifact_root / "staging").iterdir()) == []


@pytest.mark.parametrize("state", ["failed", "processing", "queued"])
def test_unsuccessful_response_is_not_retained(retention, state):
    response = rich_response()
    response["status"]["state"] = state
    with pytest.raises(ArtifactError, match="parse_failed"):
        retain(retention, response)
    assert list(retention.store.documents.iterdir()) == []


def test_schema_invalid_and_nonfinite_values_are_rejected(retention):
    for response in ({}, {**rich_response(), "usage": {"duration_ms": float("nan")}}):
        with pytest.raises(ArtifactError, match="parse_failed"):
            retain(retention, response)


def duplicate_pages(*, blocks=False):
    response = rich_response()
    response["document"] = {
        "page_count": 2,
        "pages": [
            {
                "page_number": 1,
                **({"blocks": [{"type": "text", "text": text}]} if blocks else {"text": text}),
            }
            for text in ("Payment approved.", "Payment rejected.")
        ],
    }
    return response


@pytest.mark.parametrize("blocks", [False, True])
def test_duplicate_physical_pages_cannot_publish_ambiguous_evidence(retention, blocks):
    with pytest.raises(ArtifactError, match="parse_failed"):
        retain(retention, duplicate_pages(blocks=blocks))
    assert list(retention.store.documents.iterdir()) == []
    assert list((retention.config.artifact_root / "staging").iterdir()) == []


def test_legacy_external_artifact_with_duplicate_pages_is_corrupt(retention, monkeypatch):
    from openreading.artifacts import retention as module
    from openreading.types.response import NormalizedResponse

    def old_passages(parsed):
        for page in parsed.document.pages:
            single = parsed.model_copy(
                update={"document": parsed.document.model_copy(update={"pages": [page]})}
            )
            yield from module_passages(single)

    module_passages = module.iter_passages
    # Emulate the old writer while preserving real file hashes and acquisition bindings.
    with monkeypatch.context() as old_writer:
        old_writer.setattr(module, "validate_external_response", NormalizedResponse.model_validate)
        old_writer.setattr(module, "iter_passages", old_passages)
        receipt = retain(retention, duplicate_pages())
    with pytest.raises(ArtifactError, match="artifact_corrupt"):
        retention.load_artifact(receipt.artifact_id)


@pytest.mark.parametrize("unpaginated", [False, True])
def test_external_search_and_read_agree_for_distinct_sources(retention, unpaginated):
    response = duplicate_pages()
    if unpaginated:
        response["warnings"] = [
            {"code": "page_attribution_unavailable", "message": "Synthetic containers"}
        ]
    else:
        response["document"]["pages"][0]["page_number"] = 2
    receipt = retain(retention, response)
    for word in ("approved", "rejected"):
        hit = retention.search(receipt.artifact_id, word).hits[0]
        result = retention.read(receipt.artifact_id, [hit.evidence_id])
        assert word in result.passages[0].text
        assert hit.evidence_id.startswith("d" if unpaginated else "p")


@pytest.mark.parametrize("response", [None, [], "invalid", 1])
def test_nonobject_external_response_is_a_domain_failure(retention, response):
    with pytest.raises(ArtifactError, match="parse_failed"):
        retain(retention, response)
    assert list(retention.store.documents.iterdir()) == []


def test_external_receipt_label_cannot_add_lines_or_bidi_controls(retention):
    from openreading.artifacts.retention import retain_response

    name = "invoice\u2028extra\u2029line\u202etxt.md"
    (retention.config.input_root / name).write_bytes("synthetic 界".encode())
    receipt = retain_response(
        retention,
        name,
        rich_response(),
        source_sha256=hashlib.sha256("synthetic 界".encode()).hexdigest(),
        destination_sha256="d" * 64,
        request_sha256="e" * 64,
    )
    assert receipt.display_name == "invoiceextralinetxt.md"


def test_retention_respects_storage_limits(retention):
    retention.config = replace(
        retention.config, limits=replace(retention.config.limits, store_bytes=10)
    )
    with pytest.raises(ArtifactError, match="storage_limit"):
        retain(retention, rich_response())
    assert list(retention.store.documents.iterdir()) == []


def test_retention_cancellation_and_extraction_limit_leave_no_artifact(retention):
    import threading

    event = threading.Event()
    event.set()
    with pytest.raises(ArtifactError, match="cancelled"):
        retain(retention, rich_response(), cancelled=event)
    retention.config = replace(
        retention.config, limits=replace(retention.config.limits, extraction_bytes=8)
    )
    with pytest.raises(ArtifactError, match="extraction_too_large"):
        retain(retention, rich_response())
    assert list(retention.store.documents.iterdir()) == []


def test_complete_external_staging_wins_late_cancellation(retention, monkeypatch):
    import threading

    event = threading.Event()
    original = retention._receipt

    def receipt(*args, **kwargs):
        result = original(*args, **kwargs)
        event.set()
        return result

    monkeypatch.setattr(retention, "_receipt", receipt)
    response = rich_response()
    result = retain(retention, response, cancelled=event)
    assert event.is_set()
    assert retention.store.load_document(result.artifact_id)[2] == response
    assert list((retention.config.artifact_root / "staging").iterdir()) == []


def test_disk_failure_leaves_no_committed_artifact(retention, monkeypatch):
    import os

    def fail(*args):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(os, "rename", fail)
    with pytest.raises(ArtifactError, match="storage_limit"):
        retain(retention, rich_response())
    assert list(retention.store.documents.iterdir()) == []


def test_acquisition_tampering_is_detected(retention):
    import json

    receipt = retain(retention, rich_response())
    path = retention.store.documents / receipt.artifact_id / "manifest.json"
    value = json.loads(path.read_bytes())
    value["acquisition"]["destination_sha256"] = "f" * 64
    path.write_text(json.dumps(value))
    with pytest.raises(ArtifactError, match="artifact_corrupt"):
        retention.load_artifact(receipt.artifact_id)


def test_external_retention_limits_do_not_require_a_local_parser(tmp_path):
    from openreading.artifacts import limits

    assert hasattr(limits, "ExternalLimits"), "External retention needs explicit optional limits"
    configured = limits.ExternalLimits(source_bytes=100 * 1024**2)
    config = limits.ProfileConfig(tmp_path / "input", tmp_path / "artifacts", configured)
    assert config.docling is None
    assert configured.deadline_seconds is None
    assert configured.store_bytes is None
    assert configured.pages is None
    assert limits.ProfileLimits().deadline_seconds == 45
    for field in ("source_bytes", "extraction_bytes", "store_bytes", "pages"):
        for value in (0, -1, True, 1.5):
            with pytest.raises(ValueError):
                limits.ExternalLimits(**{field: value})
    for value in (0, -1, True, float("inf")):
        with pytest.raises(ValueError):
            limits.ExternalLimits(deadline_seconds=value)


def test_external_retention_respects_reported_page_limit(retention):
    from openreading.artifacts.limits import ExternalLimits

    retention.config = replace(retention.config, limits=ExternalLimits(pages=1))
    with pytest.raises(ArtifactError, match="input_too_large"):
        retain(retention, rich_response())
