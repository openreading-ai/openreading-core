"""General retention binds complete normalized results without inventing text evidence."""

import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from openreading.artifacts.limits import ProfileConfig
from openreading.artifacts.models import json_bytes
from openreading.artifacts.result_models import ResultError, ResultProvenance
from openreading.artifacts.results import RetainedResults
from openreading.artifacts.store import Store
from openreading.comparison import compare


@pytest.fixture
def results(tmp_path):
    root = tmp_path.resolve()
    (root / "input").mkdir()
    store = Store(ProfileConfig(root / "input", root / "store"))
    try:
        yield RetainedResults(store)
    finally:
        store.close()


def provenance(**changes):
    return ResultProvenance(
        **{
            "request_sha256": "a" * 64,
            "config_sha256": "b" * 64,
            "core_version": "test",
            "core_commit": None,
            "adapters": {"synthetic": None},
            "source_sha256": ["c" * 64],
            "subjects": {},
            **changes,
        }
    )


def response(state="succeeded", text="Literal café 界"):
    return {
        "schema_version": "0.3",
        "status": {"state": state},
        "backend": {"id": "synthetic", "type": "oss_library"},
        "document": {"text": text},
        "warnings": [{"code": "unmeasured", "message": "No measured origin"}],
    }


@pytest.mark.parametrize("state", ["succeeded", "partial", "failed"])
def test_round_trip_textless_states_preserves_bytes_and_restart(results, state):
    payload = response(state, "")
    payload["typed_fields"] = {"backend_raw": {"value": {"nullable": None}}}
    payload["backend_raw"] = {"payload": "RAW-ONLY", "encoding": "json"}
    before = copy.deepcopy(payload)
    receipt = results.publish("normalized_response", payload, provenance())
    expected = {key: value for key, value in payload.items() if key != "backend_raw"}
    loaded = RetainedResults(results.store).load(receipt.result_id)
    assert loaded.payload == expected
    assert payload == before
    assert loaded.provenance == provenance()
    assert receipt.content_sha256 == hashlib.sha256(json_bytes(loaded.wire())).hexdigest()
    assert receipt.content_bytes == len(json_bytes(loaded.wire()))
    assert set(loaded.wire()) == {"kind", "provenance", "payload"}
    raw = results.path(receipt.result_id).read_bytes()
    assert receipt.result_id == "orr1_" + hashlib.sha256(raw).hexdigest()
    assert results.path(receipt.result_id).stat().st_mode & 0o777 == 0o600
    assert results.publish("normalized_response", payload, provenance()) == receipt


def test_identity_binds_output_configuration_and_sources(results):
    original = results.publish("normalized_response", response(), provenance())
    variants = [
        results.publish("normalized_response", response(text="different"), provenance()),
        results.publish("normalized_response", response(), provenance(config_sha256="d" * 64)),
        results.publish("normalized_response", response(), provenance(request_sha256="d" * 64)),
        results.publish("normalized_response", response(), provenance(source_sha256=["d" * 64])),
        results.publish("normalized_response", response(), provenance(adapters={"synthetic": "2"})),
    ]
    assert len({r.result_id for r in [original, *variants]}) == 6


def test_compare_report_keeps_explicit_subject_mapping(results):
    inputs = [response(text="first"), response(text="second")]
    receipts = [results.publish("normalized_response", p, provenance()) for p in inputs]
    report = compare(inputs)
    subjects = {s["label"]: r.result_id for s, r in zip(report["subjects"], receipts, strict=True)}
    origin = provenance(subjects=subjects)
    receipt = results.publish("comparison_report", report, origin)
    loaded = results.load(receipt.result_id)
    assert loaded.payload == report
    assert loaded.provenance.subjects == subjects
    with pytest.raises(ResultError, match="invalid_result"):
        results.publish("comparison_report", report, provenance())
    foreign = {label: "orr1_" + "f" * 64 for label in subjects}
    with pytest.raises(ResultError, match="result_not_found"):
        results.publish("comparison_report", report, provenance(subjects=foreign))


def test_grant_isolation_and_copied_record_refusal(results, tmp_path):
    receipt = results.publish("normalized_response", response(), provenance())
    other_input = tmp_path.resolve() / "other-input"
    other_input.mkdir()
    other_store = Store(ProfileConfig(other_input, results.store.config.artifact_root))
    try:
        other = RetainedResults(other_store)
        with pytest.raises(ResultError, match="result_not_found"):
            other.load(receipt.result_id)
        other.path(receipt.result_id).write_bytes(results.path(receipt.result_id).read_bytes())
        with pytest.raises(ResultError, match="result_corrupt"):
            other.load(receipt.result_id)
    finally:
        other_store.close()


@pytest.mark.parametrize("damage", ["byte", "symlink", "directory", "fifo", "grant", "format"])
def test_corrupt_records_fail_closed(results, damage, tmp_path):
    receipt = results.publish("normalized_response", response(), provenance())
    path = results.path(receipt.result_id)
    raw = path.read_bytes()
    path.unlink()
    if damage == "byte":
        path.write_bytes(raw.replace(b"Literal", b"Changed"))
    elif damage == "symlink":
        target = tmp_path / "outside.json"
        target.write_bytes(raw)
        path.symlink_to(target)
    elif damage == "directory":
        path.mkdir()
    elif damage == "fifo":
        os.mkfifo(path)
    else:
        record = json.loads(raw)
        record["input_grant_sha256" if damage == "grant" else "format"] = (
            "f" * 64 if damage == "grant" else "retained-result.v99.0"
        )
        path.write_bytes(json_bytes(record))
    with pytest.raises(ResultError, match="result_corrupt"):
        results.load(receipt.result_id)


@pytest.mark.parametrize("identifier", ["../secret", "or1_" + "a" * 64, "orr1_" + "z" * 64])
def test_invalid_identifiers_never_become_paths(results, identifier):
    with pytest.raises(ResultError, match="result_not_found"):
        results.load(identifier)


def test_invalid_payload_does_not_publish(results):
    for payload in [{}, response(state="invented")]:
        with pytest.raises(ResultError, match="invalid_result"):
            results.publish("normalized_response", payload, provenance())
    assert list(results.root.iterdir()) == []


def test_concurrent_publish_is_identical_and_complete(results):
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(
            pool.map(
                lambda _: results.publish("normalized_response", response(), provenance()), range(8)
            )
        )
    assert len({r.result_id for r in receipts}) == 1
    assert results.load(receipts[0].result_id).payload == response()
    assert len(list(results.root.iterdir())) == 1


def test_typed_fields_without_document_text_are_retained(results):
    payload = response()
    payload["document"] = {}
    payload["typed_fields"] = {"total": {"value": 42}}
    receipt = results.publish("normalized_response", payload, provenance())
    assert results.load(receipt.result_id).payload == payload


def test_corrupt_existing_publication_is_never_replaced(results):
    receipt = results.publish("normalized_response", response(), provenance())
    path = results.path(receipt.result_id)
    path.write_bytes(b"broken")
    with pytest.raises(ResultError, match="result_corrupt"):
        results.publish("normalized_response", response(), provenance())
    assert path.read_bytes() == b"broken"
    assert not list(results.root.glob(".openreading-export-*.tmp"))


def test_retrieval_rejects_noncanonical_or_invalid_records_even_with_correct_digest(results):
    receipt = results.publish("normalized_response", response(), provenance())
    record = json.loads(results.path(receipt.result_id).read_bytes())
    variants = [json.dumps(record, indent=2).encode()]
    record["content"]["payload"]["status"]["state"] = "invented"
    variants.append(json_bytes(record))
    for data in variants:
        identifier = "orr1_" + hashlib.sha256(data).hexdigest()
        results.path(identifier).write_bytes(data)
        with pytest.raises(ResultError, match="result_corrupt"):
            results.load(identifier)


def test_publish_write_failure_leaves_no_visible_result(results, monkeypatch):
    from openreading.artifacts import delivery
    from openreading.artifacts.limits import ArtifactError

    def fail(*args, **kwargs):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(delivery.os, "link", fail)
    with pytest.raises(ArtifactError, match="storage_limit"):
        results.publish("normalized_response", response(), provenance())
    assert list(results.root.iterdir()) == []


def test_report_input_must_be_normalized_and_legacy_artifacts_stay_unchanged(results, tmp_path):
    from tests.test_artifact_document import retain, rich_response

    service, legacy_id, _ = retain(tmp_path, rich_response())
    try:
        legacy_root = service.store.documents / legacy_id
        before = {p.name: p.read_bytes() for p in legacy_root.iterdir()}
        first = {k: v for k, v in rich_response().items() if k != "backend_raw"}
        second = response()
        normalized = results.publish("normalized_response", second, provenance())
        report = compare([first, second])
        subjects = {
            report["subjects"][0]["label"]: legacy_id,
            report["subjects"][1]["label"]: normalized.result_id,
        }
        retained = results.publish("comparison_report", report, provenance(subjects=subjects))
        assert results.load(retained.result_id).payload == report
        subjects[report["subjects"][1]["label"]] = retained.result_id
        with pytest.raises(ResultError, match="invalid_result"):
            results.publish("comparison_report", report, provenance(subjects=subjects))
        assert {p.name: p.read_bytes() for p in legacy_root.iterdir()} == before
    finally:
        service.close()


def test_schema_models_and_external_payload_validation(results):
    import jsonschema
    from pydantic import TypeAdapter
    from referencing import Registry, Resource

    from openreading import schemas
    from openreading.artifacts.result_models import ResultPayload, ResultRecord

    registry = Registry().with_resources(
        (doc["$id"], Resource.from_contents(doc))
        for doc in [schemas.response_schema(), schemas.comparison_report_schema()]
    )
    for schema, model in [
        (schemas.retained_result_schema(), ResultRecord),
        (schemas.result_tool_schema(), TypeAdapter(ResultPayload)),
    ]:
        generated = model.model_json_schema() if isinstance(model, type) else model.json_schema()
        assert {k: v for k, v in schema.items() if k not in {"$id", "$schema"}} == generated
    receipt = results.publish("normalized_response", response(), provenance())
    record = json.loads(results.path(receipt.result_id).read_bytes())
    validator = jsonschema.Draft202012Validator(schemas.retained_result_schema(), registry=registry)
    validator.validate(record)
    record["content"]["payload"]["status"]["state"] = "invented"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(record)
